"""What the report says, shared by the Markdown report and the web UI.

Both show the same run, so the rules that decide what is shown live here once: Russian
labels, plural forms, where a quote stands in the document, which findings count, and the
verdict «can development start». The Markdown report renders these values as text; the UI
sends them to the page as data. Neither re-derives them, so the two cannot disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from reqaudit.criteria import CRITERIA, TZ_TYPES, Criterion
from reqaudit.models import (
    Block,
    BlockSource,
    Finding,
    PartRole,
    RunState,
    Severity,
    UnitKind,
)

# --- labels -----------------------------------------------------------------------------

KIND_RU = {
    UnitKind.FUNCTIONAL: "функциональное",
    UnitKind.NON_FUNCTIONAL: "нефункциональное",
    UnitKind.DATA: "данные",
    UnitKind.INTEGRATION: "интеграция",
    UnitKind.UI: "интерфейс",
    UnitKind.ACCEPTANCE_CRITERION: "критерий приёмки",
    UnitKind.API_CONTRACT: "контракт API",
    UnitKind.CONSTRAINT: "ограничение",
    UnitKind.OTHER: "прочее",
}
PART_ROLE_RU = {PartRole.MAIN: "основная спецификация",
                PartRole.CHILD_SPEC: "дочерняя спецификация",
                PartRole.APPENDIX: "приложение", PartRole.OTHER: "прочее"}
SKIP_RU = {"goal": "цели", "context": "пояснения", "metadata": "служебные сведения",
           "template": "незаполненный шаблон", "example": "примеры",
           "approval": "согласование", "empty": "пустые", "other": "прочее"}
STATUS_RU = {
    "completed": "завершён",
    "partial": "завершён частично: один из этапов упал, отчёт собран из того, что успели",
    "checks_incomplete": "завершён, но часть проверок не выполнена; список — в разделе «Сводка»",
    "input_rejected": "документ не принят",
    "running": "не завершён",
}
SEVERITY_RU = {Severity.CRITICAL: "критичный", Severity.MAJOR: "существенный",
               Severity.MINOR: "незначительный"}
DECISION_RU = {"confirmed": "Дефект остаётся: документ нужно исправить.",
               "changed": "Дефект остаётся, серьёзность изменена.",
               "resolved": "Находка снята: ответ показывает, что документ это покрывает."}

# --- words ------------------------------------------------------------------------------


def plural_form(n: int, one: str, few: str, many: str) -> str:
    """The Russian form for n: 1 дефект, 2 дефекта, 5 дефектов (21 дефект, 12 дефектов)."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def plural(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural_form(n, one, few, many)}"


_EMPHASIS = re.compile(r"\*\*|__|`")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")


def display_quote(quote: str) -> str:
    """Quote for reading: markdown markup removed, words untouched."""
    text = _EMPHASIS.sub("", quote).strip()
    text = _LIST_MARKER.sub("", text)
    if text.startswith("|"):
        text = text.strip("| ").replace(" | ", " · ")
    return text


def place(block_ids: list[str], blocks: dict[str, Block]) -> str:
    """Where the blocks stand: pages, fragments or lines, and how their text was read."""
    found = [blocks[i] for i in block_ids if i in blocks]
    if not found:
        return "—"
    anchors = [b.anchor for b in found]
    note = (", распознано по изображению"
            if any(b.source == BlockSource.VLM for b in found) else "")
    if any(b.doubtful for b in found):
        note += ", прочтения скана расходятся — сверить с оригиналом"
    pages = sorted({a.page for a in anchors if a.page})
    if pages:
        return "стр. " + ", ".join(map(str, pages)) + note
    paragraphs = sorted({a.paragraph for a in anchors if a.paragraph})
    if paragraphs:
        span = (str(paragraphs[0]) if len(paragraphs) == 1
                else f"{paragraphs[0]}–{paragraphs[-1]}")
        return f"фрагмент {span}{note}"
    lines = [a for a in anchors if a.line_start]
    if not lines:
        return "—" + note
    start = min(a.line_start for a in lines)
    end = max(a.line_end or a.line_start for a in lines)
    return (f"строка {start}" if start == end else f"строки {start}–{end}") + note


# --- what counts ------------------------------------------------------------------------

# Without these stages no requirement was checked, so there is no verdict to give.
CHECK_STAGES = frozenset({"s5", "s6", "s7"})


def has_checks(state: RunState) -> bool:
    return set(state.completed) >= CHECK_STAGES


def reported(state: RunState) -> list[Finding]:
    """The findings the report counts as defects (see Finding.in_report), remarks apart."""
    return [f for f in state.findings if f.in_report and not f.remark]


def remarks(state: RunState) -> list[Finding]:
    """True findings that change no behaviour: listed separately, not counted as defects."""
    return [f for f in state.findings if f.in_report and f.remark]


def rejected_by_gate(f: Finding) -> bool:
    """Dropped by the code gates (s7), not by the opponent and the arbiter (s6)."""
    return any(t.startswith("gate:") for t in f.trail)


# --- the verdict ------------------------------------------------------------------------

FIRST_FIXES = 7  # how many findings «fix first» lists


@dataclass
class Verdict:
    """«Can development start?» — the answer and what to fix first."""

    level: str  # "no", "conditional" or "yes"
    word: str
    text: str
    incomplete: bool
    fixes_ready: int = 0  # findings with a document fix from the author's answers
    first: list[Finding] = field(default_factory=list)


def verdict(state: RunState, confirmed: list[Finding]) -> Verdict:
    if state.profile is not None and not state.profile.is_specification:
        reason = state.profile.not_specification_reason or "требований к системе в нём нет"
        return Verdict(level="none", word="Не применимо", incomplete=False,
                       text=f"Это не спецификация требований: {reason}. Начинать по нему "
                            "разработку нельзя, потому что разрабатывать нечего: требования "
                            "к системе нужно сформулировать отдельно.")
    critical = [f for f in confirmed if f.severity == Severity.CRITICAL]
    major = [f for f in confirmed if f.severity == Severity.MAJOR]
    if critical:
        n = plural(len(critical), "критичный дефект", "критичных дефекта", "критичных дефектов")
        verb = plural_form(len(critical), "делает", "делают", "делают")
        level, word = "no", "Нет"
        text = (f"Начинать разработку и тестирование по этому документу нельзя: {n} {verb} "
                "поведение системы неопределённым или противоречивым.")
    elif major:
        n = plural(len(major), "существенный дефект", "существенных дефекта",
                   "существенных дефектов")
        verb = plural_form(len(major), "приведёт", "приведут", "приведут")
        level, word = "conditional", "С оговорками"
        text = (f"Критичных дефектов нет, но {n} {verb} к разному пониманию требований; их "
                "стоит исправить до начала разработки.")
    else:
        level, word = "yes", "Да"
        text = ("Существенных дефектов не найдено; незначительные замечания можно исправить "
                "по ходу работы.")
    return Verdict(level=level, word=word, text=text, incomplete=bool(state.check_failures),
                   fixes_ready=sum(1 for f in confirmed if f.document_fix),
                   first=(critical + major)[:FIRST_FIXES])


# --- criteria in the report -------------------------------------------------------------


def criterion_ref(code: Criterion) -> str:
    """«однозначность (ISO/IEC/IEEE 29148:2018, п. 5.2.5; IEEE Std 830-1998, п. 4.3.2)»."""
    info = CRITERIA[code]
    return f"{info.label_ru} ({info.standard_ru})" if info.standard_ru else info.label_ru


def by_task_type(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    """Findings grouped as the task's summary names critical defects: contradictory,
    unverifiable, incomplete; the rest last. Empty groups are left out."""
    groups: dict[str | None, list[Finding]] = {t: [] for t in TZ_TYPES}
    groups[None] = []
    for f in findings:
        groups[CRITERIA[f.criterion].tz_type].append(f)
    named = [(TZ_TYPES[t], groups[t]) for t in TZ_TYPES]
    return [(name, items) for name, items in [*named, ("другие", groups[None])] if items]
