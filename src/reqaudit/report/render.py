"""Markdown report in Russian, in the section order required by the task (README §6)."""

from __future__ import annotations

import re

from reqaudit import criteria
from reqaudit.models import Block, Finding, FindingStatus, PartRole, Scope, Severity, UnitKind
from reqaudit.passport import Passport
from reqaudit.stages.base import RunState

_KIND_RU = {
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
_PART_ROLE_RU = {PartRole.MAIN: "основная спецификация",
                 PartRole.CHILD_SPEC: "дочерняя спецификация",
                 PartRole.APPENDIX: "приложение", PartRole.OTHER: "прочее"}
_SKIP_RU = {"goal": "цели", "context": "пояснения", "metadata": "служебные сведения",
            "template": "незаполненный шаблон", "example": "примеры",
            "approval": "согласование", "empty": "пустые", "other": "прочее"}

_STATUS_RU = {
    "completed": "завершён",
    "partial": "завершён частично: один из этапов упал, отчёт собран из того, что успели",
    "input_rejected": "документ не принят",
    "running": "не завершён",
}
_SEVERITY_RU = {Severity.CRITICAL: "критичный", Severity.MAJOR: "существенный",
                Severity.MINOR: "незначительный"}
_ROLE_RU = {"judge": "суждения", "opponent": "оппонент", "vision": "зрительное чтение"}

_CHECK_STAGES = {"s5", "s6", "s7"}


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip() or "—"


def _table(header: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _not_done(stage: str, state: RunState) -> str:
    if any(err.stage <= stage for err in state.errors):
        return ("Этап не выполнялся: прогон остановлен раньше, причина указана "
                "в разделе «Ход прогона».")
    return (f"Этап {stage} ещё не реализован в этой версии агента. "
            "Раздел будет заполнен после его подключения.")


def _passport(p: Passport) -> str:
    if p.git_commit:
        version = f"{p.agent_version}, коммит `{p.git_commit[:12]}`"
        if p.git_dirty:
            version += " (есть незакоммиченные изменения)"
    else:
        version = f"{p.agent_version}, коммит неизвестен"
    if p.models_used:
        models = ", ".join(f"`{m}`" for m in p.models_used)
        models += f"; запросов к модели: {p.llm_calls}, ответов из кэша: {p.llm_cache_hits}"
    else:
        models = "модель в этом прогоне не вызывалась"
    title = p.document_name + (f" — «{p.document_title}»" if p.document_title else "")
    rows = [
        ["Документ", title],
        ["SHA-256 входного файла", f"`{p.input_sha256}`" if p.input_sha256 else None],
        ["Дата прогона", p.started_at],
        ["Модель", models],
        ["Версия агента", version],
        ["Профиль", p.profile],
        ["Отпечаток промптов", f"`{p.prompts_fingerprint}`"],
        ["Идентификатор прогона", f"`{p.run_id}`, артефакты в `{p.run_dir}/`"],
        ["Статус", _STATUS_RU.get(p.status, p.status)],
    ]
    return _table(["Поле", "Значение"], rows)


def _reading(state: RunState) -> str:
    intake = state.intake
    if intake is None:
        return _not_done("s0", state)
    rows = [
        ["Формат по содержимому", intake.detected_format.value],
        ["Размер", f"{intake.size_bytes} байт"],
        ["Кодировка", intake.encoding or "—"],
    ]
    if state.document:
        rows.append(["Цепочка чтения", " → ".join(state.document.reader_chain)])
        rows.append(["Блоков текста", len(state.document.blocks)])
    parts = [_table(["Параметр", "Значение"], rows)]
    warnings = intake.warnings + (state.document.warnings if state.document else [])
    if warnings:
        parts.append("Предупреждения:\n\n" + "\n".join(f"- {w}" for w in warnings))
    if state.document and state.document.issues:
        parts.append("Замечания к самому файлу:\n\n" + "\n".join(
            f"- строка {i.line}: {i.message}" if i.line else f"- {i.message}"
            for i in state.document.issues))
    if state.document is None and "s1" not in {e.stage for e in state.errors}:
        parts.append(_not_done("s1", state))
    return "\n\n".join(parts)


_EMPHASIS = re.compile(r"\*\*|__|`")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")


def display_quote(quote: str) -> str:
    """Quote for reading: markdown markup removed, words untouched."""
    text = _EMPHASIS.sub("", quote).strip()
    text = _LIST_MARKER.sub("", text)
    if text.startswith("|"):
        text = text.strip("| ").replace(" | ", " · ")
    return text


def _place(block_ids: list[str], blocks: dict[str, Block]) -> str:
    anchors = [blocks[i].anchor for i in block_ids if i in blocks]
    if not anchors:
        return "—"
    pages = sorted({a.page for a in anchors if a.page})
    if pages:
        return "стр. " + ", ".join(map(str, pages))
    start = min(a.line_start for a in anchors if a.line_start)
    end = max(a.line_end for a in anchors if a.line_end)
    return f"строка {start}" if start == end else f"строки {start}–{end}"


def _segmentation(state: RunState) -> str:
    profile, coverage = state.profile, state.coverage
    if profile is None or coverage is None or state.document is None:
        return _not_done("s2", state)
    blocks = {b.id: b for b in state.document.blocks}
    parts_text = "; ".join(
        f"«{p.name}» — {_PART_ROLE_RU[p.role]}" + (f", детализирует «{p.parent}»"
                                                    if p.parent else "")
        for p in profile.parts)
    out = [f"{profile.summary}", "",
           f"Части документа: {parts_text}.  ",
           f"Нумерация требований: {profile.numbering}.", ""]

    if state.units:
        out.append(_table(
            ["ID в отчёте", "ID в документе", "Тип", "Место", "Краткая формулировка",
             "Цитата из документа"],
            [[u.id, u.doc_label or "нет, присвоен агентом", _KIND_RU[u.kind],
              _place(u.block_ids, blocks), u.summary, f"«{display_quote(u.quote)}»"]
             for u in state.units],
        ))
    else:
        out.append("**В документе не найдено ни одного требования.** Проверять нечего; "
                   "если требования в документе есть, их не удалось распознать.")

    reasons = ", ".join(f"{_SKIP_RU.get(k, k)} — {v}"
                        for k, v in sorted(coverage.skipped_by_reason.items()))
    out += ["", f"Учтено блоков текста: {coverage.blocks_checked}. Входят в требования: "
                f"{coverage.in_units}. Не содержат требований: {coverage.skipped}"
                + (f" ({reasons})" if reasons else "") + "."]
    if coverage.unclassified:
        places = ", ".join(_place([i], blocks) for i in coverage.unclassified[:15])
        count = plural(len(coverage.unclassified), "блок", "блока", "блоков")
        out += ["", f"**Не удалось классифицировать {count}** ({places}). "
                    "Они не проверялись."]
    if coverage.dropped_units:
        out += ["", "Отброшены как не подтверждённые текстом документа:", ""]
        out += [f"- {reason}" for reason in coverage.dropped_units]
    return "\n".join(out)


def _finding(f: Finding) -> str:
    quotes = "\n".join(
        f"  > {display_quote(e.quote)}  \n  > — блок `{e.block_id}`"
        + ("" if e.verified else " (цитата не подтверждена)")
        for e in f.evidence
    )
    lines = [
        f"**{f.id}. {f.title}** — {criteria.label(f.criterion)}, {_SEVERITY_RU[f.severity]}",
        "",
        f.explanation,
        "",
        quotes,
    ]
    if f.suggestion:
        lines += ["", f"Как исправить: {f.suggestion}"]
    return "\n".join(lines)


def render_markdown(p: Passport, state: RunState, plan: list[tuple[str, str]]) -> str:
    confirmed = [f for f in state.findings if f.status == FindingStatus.CONFIRMED]
    checks_ran = set(state.completed) >= _CHECK_STAGES
    no_checks = _not_done("s5", state)
    out: list[str] = ["# Отчёт о проверке требований", ""]

    if state.errors and p.status == "input_rejected":
        err = state.errors[0]
        out += [f"> **Документ не принят.** {err.message}", ""]

    out += ["## Паспорт прогона", "", _passport(p), ""]
    out += ["## Чтение документа", "", _reading(state), ""]

    out += ["## Как агент нарезал требования", "", _segmentation(state), ""]

    per_requirement = [f for f in confirmed if f.scope == Scope.REQUIREMENT]
    out += ["## Проверка каждого требования", ""]
    out.append("\n\n".join(_finding(f) for f in per_requirement) if checks_ran else no_checks)
    out.append("")

    set_level = [f for f in confirmed if f.scope != Scope.REQUIREMENT]
    out += ["## Проверка всего набора требований", ""]
    out.append("\n\n".join(_finding(f) for f in set_level) if checks_ran else no_checks)
    out.append("")

    out += ["## Уточняющие вопросы", ""]
    if state.questions:
        out.append(_table(
            ["ID", "Кому", "Вопрос", "Приоритет", "Блокирует разработку", "Дефекты"],
            [[q.id, q.addressee, q.text, q.priority, "да" if q.blocks_development else "нет",
              ", ".join(q.finding_ids)] for q in state.questions],
        ))
    else:
        out.append(_not_done("s8", state))
    out.append("")

    out += ["## Сводка", ""]
    if checks_ran:
        critical = [f for f in confirmed if f.severity == Severity.CRITICAL]
        out.append(_table(["Показатель", "Значение"], [
            ["Требований", len(state.units)],
            ["Нарушений", len(confirmed)],
            ["Критичных", len(critical)],
        ]))
        if critical:
            out += ["", "Критичные дефекты:", ""]
            out += [f"- {f.id}: {f.title} ({criteria.label(f.criterion)})" for f in critical]
    else:
        out.append("Сводка не сформирована: проверка требований в этом прогоне не выполнялась.")
    out.append("")

    out += ["## Итог", ""]
    if checks_ran:
        out.append("Итоговый вывод формирует этап s9 после подключения проверок.")
    else:
        out.append("Вывод о готовности документа к разработке не сформирован: "
                   "проверка не выполнялась. Отсутствие замечаний в этом отчёте "
                   "не означает, что документ без дефектов.")
    out.append("")

    out += ["## Ход прогона", ""]
    errors = {e.stage: e for e in state.errors}
    rows = []
    for code, title in plan:
        if code in state.completed:
            status = "выполнен"
        elif code in errors:
            status = f"ошибка: {errors[code].message}"
        else:
            status = "не выполнялся"
        rows.append([code, title, status])
    out.append(_table(["Этап", "Название", "Результат"], rows))
    out.append("")
    return "\n".join(out)
