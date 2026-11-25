"""Markdown report in Russian, in the section order required by the task (README §6)."""

from __future__ import annotations

import re

from reqaudit import criteria
from reqaudit.models import (
    Block,
    BlockSource,
    Finding,
    FindingStatus,
    PartRole,
    Scope,
    Severity,
    UnitKind,
)
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
    "checks_incomplete": "завершён, но часть проверок не выполнена; список — в разделе «Сводка»",
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


def spend_line(p: Passport) -> str:
    """Tokens and catalogue cost of the run, in one line for the passport and the console."""
    if p.llm_calls == 0:
        return "запросов к модели не было" + (
            f" (все {p.llm_cache_hits} ответов из кэша)" if p.llm_cache_hits else "")
    line = (f"вход {p.llm_input_tokens / 1000:.1f} тыс. токенов (из кэша провайдера "
            f"{p.llm_cached_tokens / 1000:.1f} тыс.), выход {p.llm_output_tokens / 1000:.1f} "
            f"тыс.; стоимость ≈ ${p.llm_cost_usd:.2f} по справочнику цен genai-prices")
    if p.llm_unpriced_calls:
        line += f", без учёта {p.llm_unpriced_calls} вызовов моделей, которых нет в справочнике"
    return line


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
        ["Расход модели", spend_line(p)],
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
    found = [blocks[i] for i in block_ids if i in blocks]
    if not found:
        return "—"
    anchors = [b.anchor for b in found]
    note = (", распознано по изображению"
            if any(b.source == BlockSource.VLM for b in found) else "")
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


def _finding(f: Finding, blocks: dict[str, Block]) -> str:
    units = ", ".join(f.unit_ids) or "документ целиком"
    lines = [f"#### {f.id}. {f.title}", "",
             f"Критерий: {criteria.label(f.criterion)} · Серьёзность: "
             f"{_SEVERITY_RU[f.severity]} · Требования: {units}", "", f.explanation, ""]
    for e in f.evidence:
        lines += [f"> «{display_quote(e.quote)}» — {_place([e.block_id], blocks)}", ""]
    if f.suggestion:
        lines += [f"**Как исправить.** {f.suggestion}", ""]
    if f.question:
        lines += [f"**Вопрос автору.** {f.question}", ""]
    if "arbiter:kept" in f.trail:
        lines += ["*Оппонент оспаривал находку, арбитр её оставил.*", ""]
    elif "opponent:unavailable" in f.trail:
        lines += ["*Находка не проверена оппонентом: он был недоступен.*", ""]
    return "\n".join(lines).rstrip()


def _facts_note(state: RunState) -> str:
    """How code lined up the document's facts, before the reviewers judged them."""
    table = state.facts
    if table is None:
        return "Сверка значений не выполнялась."
    head = (f"Агент выписал из документа {plural(len(table.facts), 'факт', 'факта', 'фактов')} "
            "(значения, единицы, типы, диапазоны, сравнения) и сверил их кодом. ")
    if not table.conflicts:
        return head + "Расхождений между ними код не нашёл."
    lines = [head + "Расхождения, найденные кодом; решение по каждому принимал рецензент, "
                    "подтверждённые вошли в находки ниже:", ""]
    lines += [f"- {c.summary}" for c in table.conflicts]
    return "\n".join(lines)


def _per_requirement(state: RunState, confirmed: list[Finding],
                     blocks: dict[str, Block]) -> str:
    own: dict[str, list[str]] = {}
    in_set: dict[str, list[str]] = {}
    for f in confirmed:
        target = own if f.scope == Scope.REQUIREMENT else in_set
        for u in f.unit_ids:
            target.setdefault(u, []).append(f.id)
    rows = [[u.id, u.doc_label or "—", u.summary,
             ", ".join(own.get(u.id, [])) or "замечаний нет",
             ", ".join(in_set.get(u.id, [])) or "—"] for u in state.units]
    out = [_table(["ID", "ID в документе", "Требование", "Замечания к требованию",
                   "Участвует в дефектах набора"], rows)]
    details = [_finding(f, blocks) for f in confirmed if f.scope == Scope.REQUIREMENT]
    if details:
        out += ["", "### Замечания к отдельным требованиям", "", "\n\n".join(details)]
    return "\n".join(out)


def _summary(state: RunState, confirmed: list[Finding]) -> str:
    count = {s: sum(1 for f in confirmed if f.severity == s) for s in Severity}
    by_gate = sum(1 for f in state.rejected if any(t.startswith("gate:") for t in f.trail))
    rows = [
        ["Требований выделено", len(state.units)],
        ["Нарушений найдено", len(confirmed)],
        ["из них критичных", count[Severity.CRITICAL]],
        ["существенных", count[Severity.MAJOR]],
        ["незначительных", count[Severity.MINOR]],
        ["Кандидатов отклонено оппонентом и арбитром", len(state.rejected) - by_gate],
        ["Кандидатов отклонено проверкой цитат", by_gate],
    ]
    out = [_table(["Показатель", "Значение"], rows)]

    by_criterion: dict[str, int] = {}
    for f in confirmed:
        by_criterion[criteria.label(f.criterion)] = by_criterion.get(
            criteria.label(f.criterion), 0) + 1
    if by_criterion:
        out += ["", _table(["Критерий", "Нарушений"],
                           sorted(by_criterion.items(), key=lambda kv: -kv[1]))]

    critical = [f for f in confirmed if f.severity == Severity.CRITICAL]
    if critical:
        out += ["", "Критичные дефекты:", ""]
        out += [f"- {f.id}: {f.title} ({criteria.label(f.criterion)})" for f in critical]
    if state.check_failures:
        out += ["", "**Не проверено** (модель не выполнила проверку, её область осталась "
                    "без оценки):", ""]
        out += [f"- {x.cell}: {x.message}" for x in state.check_failures]
    return "\n".join(out)


def _verdict(state: RunState, confirmed: list[Finding]) -> str:
    critical = [f for f in confirmed if f.severity == Severity.CRITICAL]
    major = [f for f in confirmed if f.severity == Severity.MAJOR]
    if critical:
        n = plural(len(critical), "критичный дефект", "критичных дефекта", "критичных дефектов")
        head = (f"**Нет.** Начинать разработку и тестирование по этому документу нельзя: {n} "
                "делают поведение системы неопределённым или противоречивым.")
    elif major:
        n = plural(len(major), "существенный дефект", "существенных дефекта",
                   "существенных дефектов")
        head = (f"**С оговорками.** Критичных дефектов нет, но {n} приведут к разному "
                "пониманию требований; их стоит исправить до начала разработки.")
    else:
        head = ("**Да.** Существенных дефектов не найдено; незначительные замечания можно "
                "исправить по ходу работы.")
    out = [head]
    if state.check_failures:
        out += ["", "Вывод неполный: часть проверок не выполнена, см. раздел «Сводка»."]
    first = (critical + major)[:7]
    if first:
        out += ["", "Исправить в первую очередь:", ""]
        out += [f"{n}. {f.id} — {f.title}" for n, f in enumerate(first, 1)]
    return "\n".join(out)


def _rejected(state: RunState) -> str:
    rows = []
    for f in state.rejected:
        who = ("проверка цитат" if any(t.startswith("gate:") for t in f.trail)
               else "оппонент и арбитр")
        rows.append([f.title, criteria.label(f.criterion), who, (f.review_note or "")[:300]])
    return _table(["Кандидат", "Критерий", "Кем отклонён", "Почему"], rows)


def render_markdown(p: Passport, state: RunState, plan: list[tuple[str, str]]) -> str:
    confirmed = [f for f in state.findings if f.status == FindingStatus.CONFIRMED]
    checks_ran = set(state.completed) >= _CHECK_STAGES
    no_checks = _not_done("s5", state)
    blocks = {b.id: b for b in state.document.blocks} if state.document else {}
    out: list[str] = ["# Отчёт о проверке требований", ""]

    if state.errors and p.status == "input_rejected":
        err = state.errors[0]
        out += [f"> **Документ не принят.** {err.message}", ""]

    out += ["## Паспорт прогона", "", _passport(p), ""]
    if p.setup_notes:
        out += ["Настройка прогона:", ""] + [f"- {n}" for n in p.setup_notes] + [""]
    out += ["## Чтение документа", "", _reading(state), ""]

    out += ["## Как агент нарезал требования", "", _segmentation(state), ""]

    out += ["## Проверка каждого требования", ""]
    out.append(_per_requirement(state, confirmed, blocks) if checks_ran else no_checks)
    out.append("")

    set_level = [f for f in confirmed if f.scope != Scope.REQUIREMENT]
    out += ["## Проверка всего набора требований", ""]
    if checks_ran:
        out += [_facts_note(state), ""]
        out.append("\n\n".join(_finding(f, blocks) for f in set_level)
                   or "Дефектов набора требований не найдено.")
    else:
        out.append(no_checks)
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
        out.append(_summary(state, confirmed))
    else:
        out.append("Сводка не сформирована: проверка требований в этом прогоне не выполнялась.")
    out.append("")

    out += ["## Итог", ""]
    if checks_ran:
        out.append(_verdict(state, confirmed))
    else:
        out.append("Вывод о готовности документа к разработке не сформирован: "
                   "проверка не выполнялась. Отсутствие замечаний в этом отчёте "
                   "не означает, что документ без дефектов.")
    out.append("")

    if checks_ran and state.rejected:
        out += ["## Приложение. Отклонённые кандидаты", "",
                "Находки, которые рецензент предложил, но оппонент с арбитром или проверка "
                "цитат не пропустили в отчёт.", "", _rejected(state), ""]

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
