"""Markdown report in Russian, in the section order required by the task (README §6)."""

from __future__ import annotations

from reqaudit import criteria
from reqaudit.models import Finding, FindingStatus, Scope, Severity
from reqaudit.passport import Passport
from reqaudit.stages.base import RunState

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


def _finding(f: Finding) -> str:
    quotes = "\n".join(
        f"  > {e.quote}  \n  > — блок `{e.block_id}`"
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

    out += ["## Как агент нарезал требования", ""]
    if state.units:
        out.append(_table(
            ["ID в отчёте", "ID в документе", "Раздел", "Краткая формулировка"],
            [[u.id, u.doc_label or "нет, присвоен агентом", u.part, u.summary]
             for u in state.units],
        ))
    else:
        out.append(_not_done("s2", state))
    out.append("")

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
