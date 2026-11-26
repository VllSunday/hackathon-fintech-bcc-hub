"""Markdown report in Russian, in the section order required by the task (README §6)."""

from __future__ import annotations

from reqaudit import criteria
from reqaudit.models import (
    Block,
    Finding,
    RunState,
    Scope,
    Severity,
)
from reqaudit.passport import Passport
from reqaudit.report.views import (
    DECISION_RU,
    KIND_RU,
    PART_ROLE_RU,
    SEVERITY_RU,
    SKIP_RU,
    STATUS_RU,
    by_task_type,
    criterion_ref,
    display_quote,
    has_checks,
    place,
    plural,
    rejected_by_gate,
    reported,
    verdict,
)


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
        ["Статус", STATUS_RU.get(p.status, p.status)],
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


def _segmentation(state: RunState) -> str:
    profile, coverage = state.profile, state.coverage
    if profile is None or coverage is None or state.document is None:
        return _not_done("s2", state)
    blocks = {b.id: b for b in state.document.blocks}
    parts_text = "; ".join(
        f"«{p.name}» — {PART_ROLE_RU[p.role]}" + (f", детализирует «{p.parent}»"
                                                    if p.parent else "")
        for p in profile.parts)
    out = [f"{profile.summary}", "",
           f"Части документа: {parts_text}.  ",
           f"Нумерация требований: {profile.numbering}.", ""]

    if state.units:
        out.append(_table(
            ["ID в отчёте", "ID в документе", "Тип", "Место", "Краткая формулировка",
             "Цитата из документа"],
            [[u.id, u.doc_label or "нет, присвоен агентом", KIND_RU[u.kind],
              place(u.block_ids, blocks), u.summary, f"«{display_quote(u.quote)}»"]
             for u in state.units],
        ))
    else:
        out.append("**В документе не найдено ни одного требования.** Проверять нечего; "
                   "если требования в документе есть, их не удалось распознать.")

    reasons = ", ".join(f"{SKIP_RU.get(k, k)} — {v}"
                        for k, v in sorted(coverage.skipped_by_reason.items()))
    out += ["", f"Учтено блоков текста: {coverage.blocks_checked}. Входят в требования: "
                f"{coverage.in_units}. Не содержат требований: {coverage.skipped}"
                + (f" ({reasons})" if reasons else "") + "."]
    if coverage.unclassified:
        places = ", ".join(place([i], blocks) for i in coverage.unclassified[:15])
        count = plural(len(coverage.unclassified), "блок", "блока", "блоков")
        out += ["", f"**Не удалось классифицировать {count}** ({places}). "
                    "Они не проверялись."]
    if coverage.obligations_outside:
        count = plural(len(coverage.obligations_outside), "блок", "блока", "блоков")
        out += ["", f"**Сверка вторым способом:** {count} с явным обязательством не вошли "
                    "в требования; их проверяет общая проверка документа:", ""]
        out += [f"- {place([i], blocks)}: «{word}»"
                for i, word in coverage.obligations_outside.items()]
    if coverage.dropped_units:
        out += ["", "Отброшены как не подтверждённые текстом документа:", ""]
        out += [f"- {reason}" for reason in coverage.dropped_units]
    if state.plan_notes:
        out += ["", "Решения при планировании проверок:", ""]
        out += [f"- {note}" for note in state.plan_notes]
    return "\n".join(out)


def _finding(f: Finding, blocks: dict[str, Block]) -> str:
    units = ", ".join(f.unit_ids) or "документ целиком"
    lines = [f"#### {f.id}. {f.title}", "",
             f"Критерий: {criterion_ref(f.criterion)} · Серьёзность: "
             f"{SEVERITY_RU[f.severity]} · Требования: {units}", "", f.explanation, ""]
    for e in f.evidence:
        lines += [f"> «{display_quote(e.quote)}» — {place([e.block_id], blocks)}", ""]
    if f.suggestion:
        lines += [f"**Как исправить.** {f.suggestion}", ""]
    if f.merged:
        lines += ["**Сведено в эту находку** (редактор счёл это тем же дефектом):", ""]
        lines += [f"- {m.title} — {criterion_ref(m.criterion)}, {SEVERITY_RU[m.severity]}. "
                  f"{m.explanation}" for m in f.merged]
        lines += [""]
    if f.question:
        lines += [f"**Вопрос автору.** {f.question}", ""]
    if f.author_answer:
        lines += [f"**Ответ автора.** {f.author_answer}", ""]
        decision = DECISION_RU.get(f.answer_decision or "", "")
        if decision:
            note = f" {f.answer_note}" if f.answer_note else ""
            lines += [f"**После ответа.** {decision}{note}", ""]
        if f.document_fix:
            lines += [f"**Правка документа по ответу.** {f.document_fix}", ""]
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
    by_gate = sum(1 for f in state.rejected if rejected_by_gate(f))
    rows = [
        ["Требований выделено", len(state.units)],
        ["Нарушений найдено", len(confirmed)],
        ["из них критичных", count[Severity.CRITICAL]],
        ["существенных", count[Severity.MAJOR]],
        ["незначительных", count[Severity.MINOR]],
        ["Кандидатов отклонено оппонентом и арбитром", len(state.rejected) - by_gate],
        ["Кандидатов отклонено проверкой цитат", by_gate],
    ]
    if state.markers:
        judged = sum(1 for m in state.markers if m.defect)
        rows.append(["Слов-маркеров найдено кодом / признано дефектом рецензентом",
                     f"{len(state.markers)} / {judged}"])
    answered = [q for q in state.questions if q.answer]
    if answered:
        resolved = sum(1 for f in state.findings if f.resolved)
        rows += [["Вопросов с ответом автора", f"{len(answered)} из {len(state.questions)}"],
                 ["Находок снято по ответам автора", resolved]]
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
        out += ["", "Критичные дефекты — противоречивые, непроверяемые и неполные формулировки "
                    "и другие, без которых нельзя начинать разработку:", ""]
        for kind, items in by_task_type(critical):
            out.append(f"- **{kind.capitalize()}:**")
            out += [f"  - {f.id}: {f.title} ({criteria.label(f.criterion)})" for f in items]
    if state.check_failures:
        out += ["", "**Не проверено** (модель не выполнила проверку, её область осталась "
                    "без оценки):", ""]
        out += [f"- {x.cell}: {x.message}" for x in state.check_failures]
    return "\n".join(out)


def _verdict(state: RunState, confirmed: list[Finding]) -> str:
    v = verdict(state, confirmed)
    out = [f"**{v.word}.** {v.text}"]
    if v.incomplete:
        out += ["", "Вывод неполный: часть проверок не выполнена, см. раздел «Сводка»."]
    if v.fixes_ready:
        count = plural(v.fixes_ready, "находки", "находок", "находок")
        out += ["", f"По ответам автора готовы правки текста для {count}: они приведены у "
                    "каждой находки. После их внесения документ стоит проверить снова."]
    if v.first:
        out += ["", "Исправить в первую очередь:", ""]
        out += [f"{n}. {f.id} — {f.title}" for n, f in enumerate(v.first, 1)]
    return "\n".join(out)


def _rejected(state: RunState) -> str:
    rows = []
    for f in state.rejected:
        who = ("проверка цитат" if rejected_by_gate(f)
               else "оппонент и арбитр")
        rows.append([f.title, criteria.label(f.criterion), who, (f.review_note or "")[:300]])
    return _table(["Кандидат", "Критерий", "Кем отклонён", "Почему"], rows)


def render_markdown(p: Passport, state: RunState, plan: list[tuple[str, str]]) -> str:
    confirmed = reported(state)
    resolved = [f for f in state.findings if f.resolved]
    checks_ran = has_checks(state)
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
            ["ID", "Кому", "Вопрос", "Приоритет", "Блокирует разработку", "Дефекты",
             "Ответ автора"],
            [[q.id, q.addressee, q.text, q.priority, "да" if q.blocks_development else "нет",
              ", ".join(q.finding_ids), q.answer or "—"] for q in state.questions],
        ))
        if not any(q.answer for q in state.questions):
            out += ["", "Ответы можно вписать в файл `<отчёт>.answers.md`, который агент "
                        "положил рядом с отчётом, и запустить проверку снова с "
                        "`--answers <файл>`, либо ответить в терминале с `--ask`: агент "
                        "перепроверит связанные находки."]
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

    if resolved:
        out += ["## Сняты после ответов автора", "",
                "\n\n".join(_finding(f, blocks) for f in resolved), ""]

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
        elif code in state.skipped:
            status = "не требовался"
        else:
            status = "не выполнялся"
        rows.append([code, title, status])
    out.append(_table(["Этап", "Название", "Результат"], rows))
    out.append("")
    return "\n".join(out)
