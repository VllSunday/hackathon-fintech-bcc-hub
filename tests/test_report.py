"""The report says the same thing in every place: a row's comment agrees with its verdict,
the acceptance table shows what the check said, a replayed run names its original cost."""

import json
import re
from dataclasses import replace
from pathlib import Path

from fakes import fake_team, honest_set_review

from reqaudit.config import Settings
from reqaudit.pipeline import run_pipeline

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"
_ROW = re.compile(r"^\| (R-\d{3}) \|.*$", re.MULTILINE)


def run(tmp_path: Path, model, name: str = "report.md"):
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(ORGANIZERS_DOC, tmp_path / name, settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    passport = json.loads((result.run_dir / "passport.json").read_text(encoding="utf-8"))
    return state, (tmp_path / name).read_text(encoding="utf-8"), passport


def requirement_rows(report: str) -> dict[str, str]:
    section = report.split("## Проверка каждого требования")[1].split("\n## ")[0]
    return {m.group(1): m.group(0) for m in _ROW.finditer(section)}


# --- the answer first -------------------------------------------------------------------


def test_report_opens_with_the_verdict_and_what_to_fix_first(tmp_path):
    model, _ = fake_team()
    state, report, _ = run(tmp_path, model)

    head = report.split("## Паспорт прогона")[0]
    assert "> **Можно ли начинать разработку:" in head
    assert f"Вопросов автору: {len(state['questions'])}" in head
    blocking = [q["id"] for q in state["questions"] if q["blocks_development"]]
    if blocking:
        assert ", ".join(blocking) in head
    assert "> 1. D-" in head
    # The task's own sections keep their order: the verdict is still in «Итог».
    assert report.index("## Сводка") < report.index("## Итог")


# --- the per-requirement table ----------------------------------------------------------


def test_comment_names_the_confirmed_defect_not_the_note_written_before_review(tmp_path):
    model, _ = fake_team()
    state, report, _ = run(tmp_path, model)

    own = [f for f in state["findings"] if f["scope"] == "requirement"]
    assert own
    rows = requirement_rows(report)
    for f in own:
        row = rows[f["unit_ids"][0]]
        assert f"({f['id']})" in row and f"{f['id']}: {f['title']}" in row
        assert "Проверено |" not in row  # the reviewer's «all fine» note is not shown beside it
    clean = next(r for u, r in rows.items() if "| соответствует |" in r)
    assert clean.endswith("| Проверено |")  # nothing raised: the reviewer's conclusion


def test_declined_candidate_is_named_where_the_row_says_the_requirement_is_sound(tmp_path):
    model, calls = fake_team(refute=lambda title: "Непроверяемая" in title)
    state, report, _ = run(tmp_path, model)

    assert calls["arbiter"] >= 1
    declined = [f for f in state["rejected"] if "Непроверяемая" in f["title"]]
    assert declined
    rows = requirement_rows(report)
    for f in declined:
        row = rows[f["unit_ids"][0]]
        assert "| соответствует |" in row
        assert f"кандидат «{f['title']}» отклонён оппонентом и арбитром" in row


def test_criteria_shared_by_every_requirement_are_said_once(tmp_path):
    model, _ = fake_team()
    _, report, _ = run(tmp_path, model)

    section = report.split("## Проверка каждого требования")[1].split("\n## ")[0]
    assert section.count("однозначность, проверяемость") == 1
    assert "Проверено по критериям (каждое требование): однозначность" in section
    assert "| ID | ID в документе | Требование | Нарушено |" in section


# --- acceptance coverage ----------------------------------------------------------------


def acceptance_review(tests: list[str]):
    def review(prompt: str) -> dict:
        answer = honest_set_review(prompt)
        if "Check: Критерии приёмки." in prompt:
            first = re.search(r"^\[(b\d{4})\] ", prompt, re.MULTILINE).group(1)
            answer["checklist"] = [{"unit_id": "R-002", "item": "Покупка доводит расходы до "
                                    "порога", "block_id": first, "status": "ok",
                                    "note": "Результат следует из правила.", "tests": tests}]
        return answer
    return review


def test_acceptance_table_lists_requirements_with_and_without_a_criterion(tmp_path):
    model, _ = fake_team(set_review=acceptance_review(["R-001", "R-999"]))
    state, report, _ = run(tmp_path, model)

    # Code keeps only requirement ids that exist; the unknown one never reaches the report.
    assert [a["tests"] for a in state["acceptance"]] == [["R-001"]]
    section = report.split("### Покрытие требований критериями приёмки")[1].split("\n#### ")[0]
    rows = {m.group(1): m.group(0) for m in _ROW.finditer(section)}
    assert rows["R-001"].endswith("| R-002 — результат следует из правил |")
    assert rows["R-003"].endswith("| нет |")
    bare = re.search(r"Без критерия приёмки: (\d+) из (\d+) — (.*)\.", section)
    assert int(bare.group(2)) == len(rows) and int(bare.group(1)) == len(rows) - 1
    assert "R-001" not in bare.group(3) and "R-003" in bare.group(3)


def test_no_acceptance_table_when_the_check_did_not_say_what_criteria_test(tmp_path):
    model, _ = fake_team()  # its checklist has no `tests`
    state, report, _ = run(tmp_path, model)

    assert state["acceptance"] and not any(a["tests"] for a in state["acceptance"])
    assert "Покрытие требований критериями приёмки" not in report


# --- the passport of a run replayed from the cache ---------------------------------------


def test_replayed_run_states_what_its_cached_answers_cost_at_first(tmp_path):
    model, _ = fake_team()
    _, first_report, first = run(tmp_path, model, "first.md")
    _, report, passport = run(tmp_path, model, "second.md")

    assert first["llm_calls"] > 0 and first["llm_replayed_requests"] == 0
    assert "Первично эти ответы получены" not in first_report
    assert passport["llm_calls"] == 0 and passport["llm_cache_hits"] > 0
    assert passport["llm_replayed_requests"] >= passport["llm_cache_hits"]
    assert passport["llm_replayed_output_tokens"] == first["llm_output_tokens"] > 0
    assert passport["llm_replayed_from"] and passport["llm_replayed_to"]
    line = next(x for x in report.splitlines() if x.startswith("| Расход модели |"))
    assert f"все {passport['llm_cache_hits']} ответов взяты из кэша" in line
    assert f"получены от модели {passport['llm_replayed_requests']} запросами" in line
