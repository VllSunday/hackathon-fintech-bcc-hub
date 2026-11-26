import json
from dataclasses import replace
from pathlib import Path

from fakes import _TO_CHECK, _finding, fake_team, honest_review
from pydantic_ai.exceptions import ModelQuotaExceededError
from pydantic_ai.models.function import FunctionModel

from reqaudit.config import Settings
from reqaudit.models import Cell, CellKind, Evidence, Finding, Scope, Severity
from reqaudit.pipeline import run_pipeline
from reqaudit.stages.s4_plan import plan_cells, requirement_batches
from reqaudit.stages.s7_gates import merge_duplicates

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def run(tmp_path: Path, factory, **overrides):
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]}, **overrides)
    result = run_pipeline(ORGANIZERS_DOC, tmp_path / "report.md", settings,
                          model_factory=factory)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    return result, state, report, trace


# --- planning ---------------------------------------------------------------------------


def test_cells_follow_the_profile_not_the_template():
    from reqaudit.models import Unit, UnitKind

    units = [Unit(id=f"R-{n:03d}", kind=UnitKind.FUNCTIONAL, part="A" if n < 12 else "B",
                  block_ids=[f"b{n:04d}"], quote="q", summary="s") for n in range(1, 15)]
    batches = requirement_batches(units)
    assert [len(b) for b in batches] == [8, 3, 3]  # never mixes parts in one batch

    full = plan_cells(units, has_child_spec=True, has_acceptance=True, has_api=True)
    kinds = [c.kind for c in full.cells]
    assert kinds.count(CellKind.REQUIREMENTS) == 3
    assert {CellKind.CONSISTENCY, CellKind.PARENT_CHILD, CellKind.ACCEPTANCE,
            CellKind.COMPLETENESS, CellKind.API_CONTRACT} <= set(kinds)

    bare = plan_cells(units[:1], has_child_spec=False, has_acceptance=False, has_api=False)
    assert [c.kind for c in bare.cells] == [CellKind.REQUIREMENTS, CellKind.COMPLETENESS]
    assert len(bare.notes) == 2


# --- merge ------------------------------------------------------------------------------


def _f(fid, blocks, criterion="set_consistent", severity=Severity.MAJOR, units=()):
    return Finding(id=fid, scope=Scope.SET, criterion=criterion, unit_ids=list(units),
                   title=fid, explanation="e", severity=severity,
                   evidence=[Evidence(block_id=b, quote="q") for b in blocks])


def test_same_conflict_from_two_cells_is_merged():
    kept, merged = merge_duplicates([
        _f("a", ["b1", "b2"], units=["R-001"]),
        _f("b", ["b1", "b2"], severity=Severity.CRITICAL, units=["R-002"]),
        _f("c", ["b3"], criterion="verifiable"),
    ])
    assert merged == 1 and len(kept) == 2
    survivor = next(k for k in kept if k.id == "b")
    assert survivor.severity == Severity.CRITICAL
    assert survivor.unit_ids == ["R-001", "R-002"]


# --- the whole flow ---------------------------------------------------------------------


def test_defects_reach_the_report_with_verdict_and_questions(tmp_path):
    model, calls = fake_team()
    result, state, report, _ = run(tmp_path, lambda _id: model)

    assert result.exit_code == 0, report
    assert calls["opponent"] >= 1 and calls["arbiter"] == 0
    ids = [f["id"] for f in state["findings"]]
    assert ids[0] == "D-001" and len(ids) == len(set(ids))
    assert state["findings"][0]["severity"] == "critical"  # sorted by severity
    assert all(e["verified"] for f in state["findings"] for e in f["evidence"])

    assert "#### D-001." in report
    assert "## Итог" in report and "**Нет.**" in report
    assert "Исправить в первую очередь" in report
    assert "| Q-01 |" in report
    assert "замечаний нет" in report  # requirements without findings are listed too


def test_opponent_refutation_goes_to_arbiter_and_appendix(tmp_path):
    model, calls = fake_team(refute=lambda title: "противоречат" in title)
    _, state, report, _ = run(tmp_path, lambda _id: model)

    assert calls["arbiter"] == 1
    assert all("противоречат" not in f["title"] for f in state["findings"])
    assert any("противоречат" in f["title"] for f in state["rejected"])
    assert "## Приложение. Отклонённые кандидаты" in report
    assert "оппонент и арбитр" in report


def test_opponent_replaces_a_fix_that_would_not_remove_the_defect(tmp_path):
    better = "Указать: push отправляется не позднее 60 секунд после события."

    def fix(title):
        return ({"fix_works": False, "better_fix": better} if "Непроверяемая" in title
                else {"fix_works": True})

    model, _ = fake_team(fix=fix)
    _, state, report, trace = run(tmp_path, lambda _id: model)
    rewritten = [f for f in state["findings"] if "opponent:fix_rewritten" in f["trail"]]
    assert rewritten and all(f["suggestion"] == better for f in rewritten)
    assert f"**Как исправить.** {better}" in report
    untouched = [f for f in state["findings"] if "Непроверяемая" not in f["title"]]
    assert all(f["suggestion"] != better for f in untouched)


def test_failed_fix_needs_a_better_one(tmp_path):
    model, _ = fake_team(fix=lambda title: {"fix_works": False})
    _, _, _, trace = run(tmp_path, lambda _id: model)
    assert "fix_works is false, so give better_fix" in trace


def test_arbiter_can_keep_a_disputed_finding(tmp_path):
    model, _ = fake_team(refute=lambda title: "противоречат" in title, keep_disputed=True)
    _, state, report, _ = run(tmp_path, lambda _id: model)
    kept = [f for f in state["findings"] if "противоречат" in f["title"]]
    assert kept and "arbiter:kept" in kept[0]["trail"]
    assert "арбитр её оставил" in report


def test_invented_quote_never_reaches_the_report(tmp_path):
    def lying_review(prompt):
        answer = honest_review(prompt)
        listed = _TO_CHECK.findall(prompt)
        if listed:
            unit, blocks, _ = listed[0]
            answer["findings"].append(_finding(
                "unambiguous", [unit],
                [{"block_id": blocks.split(",")[0].strip(),
                  "quote": "Этой фразы в документе нет и никогда не было."}],
                "Выдуманный дефект"))
        return answer

    model, _ = fake_team(review=lying_review)
    _, state, report, trace = run(tmp_path, lambda _id: model)

    assert '"llm_check_retry"' in trace  # the reviewer was told about the bad quote
    assert all(f["title"] != "Выдуманный дефект" for f in state["findings"])
    gated = [f for f in state["rejected"] if f["title"] == "Выдуманный дефект"]
    assert gated and "gate:no_evidence" in gated[0]["trail"]
    assert "проверка цитат" in report


def test_unavailable_opponent_keeps_findings_and_says_so(tmp_path):
    model, _ = fake_team()

    def opponent_down(messages, info):
        if "verdicts" in info.output_tools[0].parameters_json_schema["properties"]:
            raise ModelQuotaExceededError("fake", "quota")
        return model.function(messages, info)

    result, state, report, _ = run(tmp_path, lambda _id: FunctionModel(opponent_down))
    assert result.exit_code == 3 and result.status == "checks_incomplete"
    assert state["findings"]
    assert all("opponent:unavailable" in f["trail"] for f in state["findings"]
               if f["trail"][0] == "reviewer")
    assert "не проверена оппонентом" in report
    assert "**Не проверено**" in report and "Вывод неполный" in report


def test_requirements_cell_must_review_every_requirement(tmp_path):
    def forgetful(prompt):
        answer = honest_review(prompt)
        answer["reviewed"] = answer["reviewed"][:1]
        return answer

    model, _ = fake_team(review=forgetful)
    _, _, _, trace = run(tmp_path, lambda _id: model)
    assert "`reviewed` must list every requirement" in trace


def _same_defect_twice(prompt):
    """One defect reported under two criteria: the gates cannot see they are the same."""
    answer = honest_review(prompt)
    if answer["findings"]:
        twin = dict(answer["findings"][0], criterion="conforming")
        answer["findings"].append(twin)
    return answer


def test_editor_merges_one_defect_reported_under_two_criteria(tmp_path):
    model, calls = fake_team(review=_same_defect_twice)
    _, state, report, trace = run(tmp_path, lambda _id: model)

    assert calls["editor"] == 1
    titles = [f["title"] for f in state["findings"]]
    assert len(titles) == len(set(titles))
    merged = [f for f in state["findings"] if any(t.startswith("merged:") for t in f["trail"])]
    assert merged
    covered = {i for q in state["questions"] for i in q["finding_ids"]}
    assert {f["id"] for f in state["findings"]} <= covered  # questions followed the merge
    assert [f["id"] for f in state["findings"]] == [
        f"D-{n:03d}" for n in range(1, len(state["findings"]) + 1)]


def test_editor_failure_falls_back_to_reviewer_questions(tmp_path):
    model, _ = fake_team(review=_same_defect_twice)

    def editor_down(messages, info):
        if "duplicates" in info.output_tools[0].parameters_json_schema["properties"]:
            raise ModelQuotaExceededError("fake", "quota")
        return model.function(messages, info)

    result, state, report, _ = run(tmp_path, lambda _id: FunctionModel(editor_down))
    assert result.exit_code == 3  # the report is usable, but the run says what did not run
    assert state["questions"]
    assert "редактор отчёта" in report and "Дубли не сведены" in report


def test_editor_must_cover_critical_findings_with_questions(tmp_path):
    def lazy_editor(prompt):
        return {"duplicates": [], "questions": []}

    model, calls = fake_team(editor=lazy_editor)
    _, state, _, trace = run(tmp_path, lambda _id: model)
    assert "critical and major findings without a question" in trace
    assert calls["editor"] == 3  # complaint, retry, accepted as is on the last attempt
    # The safety net restored the reviewers' questions for critical and major findings.
    serious = {f["id"] for f in state["findings"] if f["severity"] != "minor"}
    assert serious <= {i for q in state["questions"] for i in q["finding_ids"]}


def test_cell_ids_are_unique():
    plan = plan_cells([], has_child_spec=True, has_acceptance=True, has_api=True)
    ids = [c.id for c in plan.cells]
    assert len(ids) == len(set(ids))
    assert all(isinstance(c, Cell) for c in plan.cells)
