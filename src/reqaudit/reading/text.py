"""Reader for Markdown and plain text.

A tolerant line-based parser, not a full CommonMark implementation. Goals in order:
1. every non-blank line of the source ends up in exactly one block;
2. every block keeps its source line range, so quotes resolve to a line;
3. broken markup never swallows the rest of the document; it is recorded as an issue.

Block text is the source lines as written (minus invisible characters). Readers never
rewrite wording: quotes in the report must match the document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from reqaudit.models import (
    Anchor,
    Block,
    BlockKind,
    BlockSource,
    DocumentIR,
    ReadIssue,
    SourceFormat,
)
from reqaudit.reading.normalize import (
    mojibake_hits,
    normalize_newlines,
    repair_mojibake,
    strip_invisible,
)

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+•]|\d{1,3}[.)])\s+\S")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_NUMBERED_HEADING = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.*)$")
_CYRILLIC_UPPER = re.compile(r"^[А-ЯЁ0-9\s.,:;«»\"()\-–—/]+$")


def _cells_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} ячейка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} ячейки"
    return f"{n} ячеек"


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    parts = re.split(r"(?<!\\)\|", inner)
    return [p.strip().replace("\\|", "|") for p in parts]


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") or stripped.count("|") >= 2


@dataclass
class _Builder:
    lines: list[str]
    flavor: SourceFormat
    blocks: list[Block] = field(default_factory=list)
    issues: list[ReadIssue] = field(default_factory=list)
    sections: list[tuple[int, str]] = field(default_factory=list)

    def _path(self) -> list[str]:
        return [title for _, title in self.sections]

    def add(self, kind: BlockKind, start: int, end: int, *, text: str | None = None,
            hidden: bool = False, table_header: list[str] | None = None) -> Block:
        block = Block(
            id=f"b{len(self.blocks) + 1:04d}",
            kind=kind,
            text=text if text is not None else "\n".join(self.lines[start:end + 1]).strip("\n"),
            anchor=Anchor(line_start=start + 1, line_end=end + 1, section_path=self._path()),
            source=BlockSource.PLAIN_TEXT,
            hidden=hidden,
            table_header=table_header,
        )
        self.blocks.append(block)
        return block

    def issue(self, kind: str, message: str, line: int | None = None,
              block: Block | None = None) -> None:
        self.issues.append(ReadIssue(kind=kind, message=message,
                                     line=line + 1 if line is not None else None,
                                     block_id=block.id if block else None))

    def heading(self, level: int, title: str, start: int, end: int) -> None:
        while self.sections and self.sections[-1][0] >= level:
            self.sections.pop()
        self.sections.append((level, title.strip()))
        self.add(BlockKind.HEADING, start, end)

    # --- block starters -----------------------------------------------------------------

    def _starts_block(self, i: int) -> bool:
        line = self.lines[i]
        return bool(
            not line.strip()
            or _FENCE.match(line)
            or _ATX.match(line)
            or _RULE.match(line)
            or _LIST.match(line)
            or line.lstrip().startswith("<!--")
            or (_is_table_line(line) and self._table_ahead(i))
        )

    def _table_ahead(self, i: int) -> bool:
        if i + 1 < len(self.lines) and _TABLE_SEP.match(self.lines[i + 1]):
            return True
        # A run of pipe-framed lines without a separator row is still a table, only broken.
        return (self.lines[i].strip().startswith("|") and i + 1 < len(self.lines)
                and self.lines[i + 1].strip().startswith("|"))

    def _closing_fence(self, i: int, marker: str) -> int | None:
        for j in range(i + 1, len(self.lines)):
            stripped = self.lines[j].strip()
            if stripped.startswith(marker[0] * len(marker)) and set(stripped) <= {marker[0]}:
                return j
        return None

    # --- parse ---------------------------------------------------------------------------

    def parse(self) -> None:
        i, n = 0, len(self.lines)
        while i < n:
            line = self.lines[i]
            if not line.strip():
                i += 1
                continue

            if fence := _FENCE.match(line):
                close = self._closing_fence(i, fence.group(1))
                if close is not None:
                    self.add(BlockKind.CODE, i, close)
                    i = close + 1
                    continue
                self.issue("unclosed_code_block",
                           f"Блок кода, открытый в строке {i + 1}, не закрыт. "
                           "Остаток документа прочитан как обычный текст.", line=i)
                self.add(BlockKind.PARAGRAPH, i, i)
                i += 1
                continue

            if line.lstrip().startswith("<!--"):
                end = i
                while end < n and "-->" not in self.lines[end]:
                    end += 1
                if end == n:
                    end = n - 1
                    self.issue("unclosed_comment",
                               f"HTML-комментарий в строке {i + 1} не закрыт.", line=i)
                block = self.add(BlockKind.PARAGRAPH, i, end, hidden=True)
                self.issue("hidden_text",
                           "HTML-комментарий не виден в отображаемом документе.", line=i,
                           block=block)
                i = end + 1
                continue

            if atx := _ATX.match(line):
                self.heading(len(atx.group(1)), atx.group(2), i, i)
                i += 1
                continue

            if _RULE.match(line):
                i += 1
                continue

            if _is_table_line(line) and self._table_ahead(i):
                i = self._table(i)
                continue

            if _LIST.match(line):
                i = self._list_item(i)
                continue

            i = self._paragraph(i)

    def _table(self, i: int) -> int:
        n = len(self.lines)
        header = _cells(self.lines[i])
        has_separator = i + 1 < n and bool(_TABLE_SEP.match(self.lines[i + 1]))
        if not has_separator:
            self.issue("table_without_separator",
                       f"Таблица в строке {i + 1} без строки-разделителя под заголовком.", line=i)
        self.add(BlockKind.TABLE, i, i, table_header=header)
        j = i + 2 if has_separator else i + 1
        while j < n and self.lines[j].strip() and _is_table_line(self.lines[j]):
            if _TABLE_SEP.match(self.lines[j]):
                j += 1
                continue
            row = self.add(BlockKind.TABLE_ROW, j, j, table_header=header)
            if (count := len(_cells(self.lines[j]))) != len(header):
                self.issue("table_column_mismatch",
                           f"В строке {j + 1} таблицы {_cells_word(count)}, "
                           f"а в заголовке {_cells_word(len(header))}.", line=j, block=row)
            j += 1
        return j

    def _list_item(self, i: int) -> int:
        n = len(self.lines)
        j = i + 1
        while j < n and self.lines[j].strip() and not self._starts_block(j):
            j += 1
        self.add(BlockKind.LIST_ITEM, i, j - 1)
        return j

    def _paragraph(self, i: int) -> int:
        n = len(self.lines)
        j = i + 1
        while j < n and self.lines[j].strip():
            if self.flavor == SourceFormat.MARKDOWN and _SETEXT.match(self.lines[j]):
                level = 1 if self.lines[j].strip().startswith("=") else 2
                self.heading(level, " ".join(s.strip() for s in self.lines[i:j]), i, j)
                return j + 1
            if self._starts_block(j):
                break
            j += 1

        if self.flavor == SourceFormat.TEXT and j == i + 1:
            level = self._plain_heading_level(self.lines[i])
            if level:
                self.heading(level, self.lines[i].strip(), i, i)
                return j
        self.add(BlockKind.PARAGRAPH, i, j - 1)
        return j

    @staticmethod
    def _plain_heading_level(line: str) -> int | None:
        """Headings in plain text: '2.1 Функциональные требования' or a short all-caps line."""
        stripped = line.strip()
        if len(stripped) > 90 or stripped.endswith((".", ",", ";", ":")):
            return None
        if numbered := _NUMBERED_HEADING.match(stripped):
            return numbered.group(1).count(".") + 1
        if _CYRILLIC_UPPER.match(stripped) and sum(ch.isalpha() for ch in stripped) >= 4:
            return 1
        return None


def read_text_document(text: str, flavor: SourceFormat) -> DocumentIR:
    issues: list[ReadIssue] = []
    text = normalize_newlines(text)

    text, invisible = strip_invisible(text)
    if invisible:
        issues.append(ReadIssue(kind="invisible_characters",
                                message=f"В тексте {invisible} невидимых символов "
                                        "(мягкие переносы, символы нулевой ширины); удалены."))

    text, repaired = repair_mojibake(text)
    if repaired:
        issues.append(ReadIssue(kind="mojibake_repaired",
                                message="Текст был сохранён в двойной кодировке "
                                        f"(UTF-8, прочитанный как {repaired}); восстановлен."))
    elif mojibake_hits(text) >= 5:
        issues.append(ReadIssue(kind="mojibake",
                                message="В тексте есть фрагменты с испорченной кодировкой, "
                                        "восстановить их автоматически не удалось."))

    lines = text.split("\n")
    builder = _Builder(lines=lines, flavor=flavor)
    builder.parse()
    issues.extend(builder.issues)

    title = next((b.anchor.section_path[-1] for b in builder.blocks
                  if b.kind == BlockKind.HEADING and b.anchor.section_path), None)
    reader = "markdown_lines" if flavor == SourceFormat.MARKDOWN else "plain_text_lines"
    return DocumentIR(title=title, format=flavor, blocks=builder.blocks, line_count=len(lines),
                      reader_chain=[reader], issues=issues)
