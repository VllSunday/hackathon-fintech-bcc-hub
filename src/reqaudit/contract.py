"""API contract check by code: one field, one data type across the parameter tables.

A REST specification describes the same field in several tables: query parameters,
request body, response. When one table says `integer` and another `string` for the same
field, the client and the server will disagree on the wire. This is a fact of the text,
not a judgement, so code finds it and the model is not asked; the opponent cannot argue
it away as "different stages of HTTP" (it did on the organisers' document, v0.2.0).

Two rows describe the same field when they name the same database field, or, when a table
has no database field column, the same parameter name in the same method section.
Only known type families are compared: an unknown word in the format column is skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reqaudit.criteria import Criterion
from reqaudit.models import (
    Block,
    BlockKind,
    Evidence,
    Finding,
    FindingStatus,
    Severity,
    Unit,
    scope_of,
)

# Header words of the columns this check reads, compared after casefold.
_NAME_COLUMNS = ("parameter", "параметр", "поле", "field", "name", "имя")
_FORMAT_COLUMNS = ("format", "формат", "type", "тип")
_DB_COLUMNS = ("db field", "поле бд", "поле в бд", "db", "column", "колонка")

# Format word -> type family. Sizes and nullability ("int32", "string?") are not types.
_FAMILIES = {
    "integer": "integer", "int": "integer", "int32": "integer", "int64": "integer",
    "long": "integer", "short": "integer", "целое": "integer", "целое число": "integer",
    "string": "string", "str": "string", "text": "string", "строка": "string",
    "boolean": "boolean", "bool": "boolean", "логический": "boolean",
    "number": "number", "decimal": "number", "float": "number", "double": "number",
    "numeric": "number", "число": "number",
    "date": "date", "datetime": "datetime", "date-time": "datetime", "timestamp": "datetime",
    "object": "object", "array": "array", "uuid": "string",
}
_EMPTY = {"", "-", "—", "–", "нет", "n/a"}
_CODE = re.compile(r"[`*]")

TYPE_RU = {"integer": "целое число", "string": "строка", "boolean": "логическое значение",
           "number": "дробное число", "date": "дата", "datetime": "дата и время",
           "object": "объект", "array": "массив"}


@dataclass(frozen=True)
class FieldRow:
    block: Block
    name: str
    db_field: str | None
    family: str
    format_text: str
    section: tuple[str, ...]


@dataclass(frozen=True)
class TypeConflict:
    """The same field with two type families in two rows."""

    field: str
    first: FieldRow
    second: FieldRow


def _cells(text: str) -> list[str]:
    inner = text.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [_CODE.sub("", c).strip() for c in inner.split("|")]


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    keys = [_CODE.sub("", h).strip().casefold() for h in header]
    for name in names:
        if name in keys:
            return keys.index(name)
    return None


def _family(text: str) -> str | None:
    word = text.strip().casefold()
    if word in _FAMILIES:
        return _FAMILIES[word]
    first = re.split(r"[\s(,/]", word, maxsplit=1)[0].rstrip("?")
    return _FAMILIES.get(first)


def field_rows(blocks: list[Block]) -> list[FieldRow]:
    """Rows of parameter tables that name a field and give it a known type."""
    rows: list[FieldRow] = []
    for block in blocks:
        if block.kind != BlockKind.TABLE_ROW or not block.table_header or block.hidden:
            continue
        header = block.table_header
        name_col = _column(header, _NAME_COLUMNS)
        format_col = _column(header, _FORMAT_COLUMNS)
        if name_col is None or format_col is None:
            continue
        cells = _cells(block.text)
        if max(name_col, format_col) >= len(cells):
            continue
        name, format_text = cells[name_col], cells[format_col]
        family = _family(format_text)
        if not name or family is None:
            continue
        db_col = _column(header, _DB_COLUMNS)
        db = cells[db_col] if db_col is not None and db_col < len(cells) else ""
        rows.append(FieldRow(block=block, name=name,
                             db_field=None if db.casefold() in _EMPTY else db,
                             family=family, format_text=format_text,
                             section=tuple(block.anchor.section_path)))
    return rows


_TABLE_SECTION = re.compile(r"параметр|parameter|запрос|request|ответ|response|query|"
                            r"path|body|header|заголов|тело", re.IGNORECASE)


def _method(section: tuple[str, ...]) -> tuple[str, ...]:
    """The section of the method a table belongs to: its path cut before the table's own
    subsection («Параметры запроса › Query-параметры», «Ответ»)."""
    for n, title in enumerate(section):
        if _TABLE_SECTION.search(title):
            return section[:n]
    return section


def find_type_conflicts(blocks: list[Block]) -> list[TypeConflict]:
    """Each field described with two type families, once, first rows in document order."""
    by_key: dict[tuple, list[FieldRow]] = {}
    for row in field_rows(blocks):
        key = ("db", row.db_field.casefold()) if row.db_field else \
            ("name", row.name.casefold(), _method(row.section))
        by_key.setdefault(key, []).append(row)
    conflicts: list[TypeConflict] = []
    for rows in by_key.values():
        first = rows[0]
        second = next((r for r in rows[1:] if r.family != first.family), None)
        if second is not None:
            label = first.name if first.name == second.name else f"{first.name} / {second.name}"
            conflicts.append(TypeConflict(field=label, first=first, second=second))
    return conflicts


def row_quote(block: Block) -> str:
    """The row as a quote: its cells without table markup, so it reads as text and still
    matches the document (the quote key ignores pipes and backticks)."""
    return " ".join(c for c in _cells(block.text) if c)


def _where(row: FieldRow) -> str:
    return f"«{row.section[-1]}»" if row.section else "таблица параметров"


def conflict_finding(conflict: TypeConflict, units: list[Unit], number: int) -> Finding:
    """A confirmed set_consistent finding for one type conflict; the code is its evidence."""
    a, b = conflict.first, conflict.second
    blocks = {a.block.id, b.block.id}
    unit_ids = sorted(u.id for u in units if blocks & set(u.block_ids))
    type_a, type_b = TYPE_RU.get(a.family, a.family), TYPE_RU.get(b.family, b.family)
    return Finding(
        id=f"contract#{number}", scope=scope_of(Criterion.SET_CONSISTENT, unit_ids),
        criterion=Criterion.SET_CONSISTENT, unit_ids=unit_ids,
        title=f"Поле «{conflict.field}»: {a.format_text} в {_where(a)}, {b.format_text} "
              f"в {_where(b)}",
        explanation=(
            f"Одно и то же поле описано с разными типами данных: в разделе {_where(a)} — "
            f"{a.format_text} ({type_a}), в разделе {_where(b)} — {b.format_text} ({type_b}). "
            "Клиент и сервер, сделанные по разным таблицам, разойдутся в формате: один "
            "отправит или будет ждать число, другой строку, и разбор ответа или сравнение "
            "значений сломается. Расхождение найдено сверкой таблиц параметров кодом, без "
            "суждения модели."),
        evidence=[Evidence(block_id=a.block.id, quote=row_quote(a.block), verified=True),
                  Evidence(block_id=b.block.id, quote=row_quote(b.block), verified=True)],
        severity=Severity.MAJOR, status=FindingStatus.CONFIRMED, trail=["code:contract"],
        suggestion=(f"Выбрать один тип для поля «{conflict.field}» и указать его во всех "
                    "таблицах и примерах JSON; если различие намеренное, прямо написать, "
                    "в каком виде поле передаётся в запросе и в ответе."),
        question=(f"Какой тип у поля «{conflict.field}» — {a.format_text} или "
                  f"{b.format_text}, и одинаков ли он в запросе и в ответе?"),
        question_to="analyst",
    )


def contract_findings(blocks: list[Block], units: list[Unit]) -> list[Finding]:
    return [conflict_finding(c, units, n)
            for n, c in enumerate(find_type_conflicts(blocks), 1)]
