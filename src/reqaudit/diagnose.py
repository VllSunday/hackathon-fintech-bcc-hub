"""Diagnosis of one finished run from its files: where it may have gone wrong.

    uv run reqaudit-diagnose runs/<id>

Reads the trace and the stage artifacts, calls no model, and prints in Russian what a
reviewer of the run should look at: spend by stage, model failures, answers that checks
sent back or accepted with problems, segmentation coverage, the path of findings through
the opponent, the gates and the editor, and patterns that usually mean a defect of the
agent rather than of the document.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reqaudit.models import RunState
from reqaudit.report.views import plural

_GAP_WORDS = re.compile(r"\b(нет|не указан\w*|не описан\w*|отсутств\w*|не определен\w*|"
                        r"не задан\w*|невозможн\w*)\b", re.IGNORECASE)


@dataclass
class Diagnosis:
    """Plain data, so a test can check the findings and the CLI can print them."""

    run_id: str
    status: str = "—"
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # what most likely needs a fix


def _stage_of(step: str) -> str:
    if step.startswith("s5["):
        cell = step[3:].rstrip("]")
        return "s5 " + re.sub(r"-\d+$", "", cell)
    return re.split(r"[_\[]", step, maxsplit=1)[0]


def _read(run_dir: Path) -> tuple[list[dict[str, Any]], RunState | None, dict[str, Any]]:
    trace_path = run_dir / "trace.jsonl"
    events = ([json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
               if line.strip()] if trace_path.exists() else [])
    state_path = run_dir / "state.json"
    state = (RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
             if state_path.exists() else None)
    artifacts = {}
    for name in ("s5_candidates", "s6_review"):
        path = run_dir / f"{name}.json"
        if path.exists():
            artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
    return events, state, artifacts


def diagnose(run_dir: Path) -> Diagnosis:
    events, state, artifacts = _read(run_dir)
    d = Diagnosis(run_id=run_dir.name)
    end = next((e for e in reversed(events) if e["event"] == "run_end"), None)
    d.status = end["status"] if end else "не завершён"
    out, warn = d.lines, d.warnings

    # --- spend and model behaviour ------------------------------------------------------
    calls = [e for e in events if e["event"] == "llm_call"]
    paid = [e for e in calls if not e.get("cache_hit")]
    by_stage: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0.0])
    for e in paid:
        row = by_stage[_stage_of(e["step"])]
        row[0] += 1
        row[1] += e.get("input_tokens", 0)
        row[2] += e.get("output_tokens", 0)
        row[3] += e.get("cost_usd") or 0.0
    total = sum(r[3] for r in by_stage.values())
    models = sorted({e.get("model", "") for e in calls})
    out += [f"## Прогон {d.run_id}: {d.status}", "",
            f"Модели: {', '.join(models) or '—'}. Вызовов: {len(paid)}, из кэша "
            f"{len(calls) - len(paid)}. Стоимость ≈ ${total:.2f}.", ""]
    if by_stage:
        out += ["| Этап | Вызовов | Вход, тыс. | Выход, тыс. | $ | Доля |",
                "|---|---|---|---|---|---|"]
        for stage, (n, i, o, c) in sorted(by_stage.items(), key=lambda kv: -kv[1][3]):
            share = f"{c / total:.0%}" if total else "—"
            out.append(f"| {stage} | {n} | {i / 1000:.1f} | {o / 1000:.1f} | {c:.2f} | {share} |")
        out_tokens = sum(r[2] for r in by_stage.values())
        in_tokens = sum(r[1] for r in by_stage.values())
        if out_tokens > in_tokens:
            out += ["", f"Выход больше входа ({out_tokens / 1000:.0f} тыс. против "
                        f"{in_tokens / 1000:.0f} тыс.): основная цена — рассуждения модели."]
        out.append("")

    retries = Counter(e.get("error") for e in events if e["event"] == "llm_retry")
    fallbacks = [e for e in events if e["event"] == "llm_fallback"]
    disabled = [e for e in events if e["event"] == "llm_model_disabled"]
    if retries or fallbacks or disabled:
        out += ["### Сбои модели", ""]
        if retries:
            out.append("Повторы: " + ", ".join(f"{k} ×{v}" for k, v in retries.most_common()) + ".")
        for e in disabled:
            warn.append(f"Модель {e.get('model')} отключена на шаге {e.get('step')}: "
                        f"{e.get('reason')}. Дальше работала запасная — сравните качество.")
        for e in fallbacks:
            out.append(f"- {e.get('step')}: переход с {e.get('model')} на следующую модель.")
        out.append("")

    sent_back = [e for e in events if e["event"] == "llm_check_retry"]
    accepted = [e for e in events if e["event"] == "llm_check_unresolved"]
    if sent_back or accepted:
        out += ["### Проверки ответов модели", "",
                f"Возвращено на исправление: {len(sent_back)}; принято с замечаниями после "
                f"последней попытки: {len(accepted)}.", ""]
        kinds = Counter()
        for e in sent_back:
            for line in e.get("complaint", "").splitlines()[1:]:
                kinds[re.sub(r"\[\d+\]|'[^']*'|«[^»]*»|\b[RFDb]-?\d+\b", "…",
                             line.strip("- "))[:90]] += 1
        out += [f"- {text} — ×{n}" for text, n in kinds.most_common(6)]
        for e in accepted:
            warn.append(f"{e.get('step')}: ответ принят с нерешённым замечанием «"
                        f"{e.get('complaint', '').splitlines()[-1].strip('- ')[:160]}». "
                        "Код отбросит неверное, но причину стоит поправить в промпте.")
        out.append("")

    if state is None:
        warn.append("Нет state.json: прогон не дошёл до отчёта, смотрите stage_error в трассе.")
        return d
    for err in state.errors:
        warn.append(f"Этап {err.stage} остановился: {err.kind}: {err.message[:200]}")

    # --- segmentation ------------------------------------------------------------------------
    cov = state.coverage
    if cov:
        out += ["### Нарезка", "",
                f"Требований: {len(state.units)}. Блоков учтено: {cov.blocks_checked}, в "
                f"требованиях {cov.in_units}, пропущено с причиной {cov.skipped}"
                f" ({', '.join(f'{k} {v}' for k, v in sorted(cov.skipped_by_reason.items()))}), "
                f"не классифицировано {len(cov.unclassified)}.", ""]
        if cov.unclassified:
            warn.append(f"Нарезка не классифицировала {len(cov.unclassified)} блоков: "
                        f"{', '.join(cov.unclassified[:10])}. Они не проверялись.")
        if cov.obligations_outside:
            warn.append(f"Вторая нарезка нашла {len(cov.obligations_outside)} блоков с явным "
                        "обязательством вне требований: "
                        + ", ".join(f"{b} «{w}»" for b, w in cov.obligations_outside.items()))
        if cov.dropped_units:
            warn.append(f"Код отбросил {len(cov.dropped_units)} требований модели как не "
                        "подтверждённые текстом.")
    for note in state.plan_notes:
        out.append(f"План: {note}")

    # --- the path of findings ------------------------------------------------------------------
    stats = {e["event"]: e for e in events
             if e["event"] in ("candidates_summary", "review_summary", "gates_summary",
                               "editor_summary")}
    cand = artifacts.get("s5_candidates", {}).get("candidates", [])
    per_cell = Counter(c.get("cell") for c in cand)
    out += ["", "### Путь находок", ""]
    if cand:
        out.append(f"Кандидатов от рецензентов: {len(cand)} ("
                   + ", ".join(f"{k} {v}" for k, v in per_cell.most_common()) + ").")
    if "review_summary" in stats:
        r = stats["review_summary"]
        out.append(f"Оппонент и арбитр: подтверждено {r.get('confirmed')}, отклонено "
                   f"{r.get('rejected')}, спорных {r.get('disputed')}.")
    if "gates_summary" in stats:
        g = stats["gates_summary"]
        out.append(f"Гейты: на входе {g.get('in')}, без цитаты {g.get('no_evidence')}, "
                   f"противоречие с одной стороной {g.get('one_sided_conflict')}, дублей "
                   f"{g.get('merged')}, прошло {g.get('out')}.")
        if g.get("merged", 0) and cand and g["merged"] / len(cand) >= 0.15:
            warn.append(f"Гейт свёл {g['merged']} дублей из {len(cand)} кандидатов: разные "
                        "ячейки находят одно и то же, а оппонент проверяет каждый дубль "
                        "отдельно. Дубли дешевле сводить до оппонента.")
    if "editor_summary" in stats:
        e = stats["editor_summary"]
        out.append(f"Редактор: {e.get('findings_before')} → {e.get('findings_after')} находок, "
                   f"вопросов {e.get('questions')}.")
    out.append("")

    reviewed = artifacts.get("s6_review", {}).get("confirmed", [])
    first = {c["id"]: c for c in cand}
    lowered = [(first[f["id"]]["severity"], f["severity"], f["title"]) for f in reviewed
               if f["id"] in first and first[f["id"]]["severity"] != f["severity"]]
    if lowered:
        out.append("Серьёзность изменена после рецензента: " + "; ".join(
            f"{a} → {b} «{t[:50]}»" for a, b, t in lowered) + ".")
    for f in state.rejected:
        if "gate:one_sided" in f.trail:
            warn.append(f"«{f.title}» отброшена гейтом как противоречие с одной стороной. "
                        + ("По заголовку это пропуск, а не противоречие: критерий выбран "
                           "неверно, и находка потеряна." if _GAP_WORDS.search(f.title)
                           else "Проверьте, не пропуск ли это под чужим критерием."))
    for f in state.findings:
        if f.criterion.value == "set_consistent" and _GAP_WORDS.search(f.title) \
                and len(f.evidence) < 2:
            warn.append(f"{f.id} «{f.title}»: похоже на пропуск под критерием "
                        "непротиворечивости.")
    covered = {fid for q in state.questions for fid in q.finding_ids}
    uncovered = [f.id for f in state.findings if f.in_report and f.severity.value != "minor"
                 and f.id not in covered]
    if uncovered:
        warn.append(f"Без уточняющего вопроса: {', '.join(uncovered)}.")
    for failure in state.check_failures:
        warn.append(f"Не выполнено: {failure.stage} «{failure.cell}»: {failure.message[:160]}")
    return d


def render(d: Diagnosis) -> str:
    text = list(d.lines)
    text += ["", "### Что стоит поправить", ""]
    if d.warnings:
        text += [f"{n}. {w}" for n, w in enumerate(d.warnings, 1)]
    else:
        text.append("Явных признаков сбоев агента не найдено.")
    count = plural(len(d.warnings), "замечание", "замечания", "замечаний")
    return "\n".join(text) + f"\n\nИтого: {count}.\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="reqaudit-diagnose",
        description="Разбор прогона по трассе и артефактам: где агент мог ошибиться. "
                    "Модель не вызывается.")
    parser.add_argument("runs", type=Path, nargs="+", help="папки прогонов runs/<id>")
    args = parser.parse_args(argv)
    for run_dir in args.runs:
        print(render(diagnose(run_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
