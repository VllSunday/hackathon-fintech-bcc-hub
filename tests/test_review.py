import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from fakes import _TO_CHECK, _finding, fake_team, honest_review, honest_set_review
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


CONFLICT = "противоречат"


def lowers(reason: str = "") -> Callable[[str], dict | None]:
    """The opponent confirms the conflict but rates it major."""
    def adjust(title):
        if CONFLICT not in title:
            return None
        return {"verdict": "adjust", "severity": "major", "severity_reason": reason}
    return adjust


def test_lowering_a_critical_finding_needs_a_reason_and_the_arbiter(tmp_path):
    model, calls = fake_team(adjust=lowers())
    _, state, _, trace = run(tmp_path, lambda _id: model)
    assert "say in severity_reason which point of the severity scale" in trace
    conflict = next(f for f in state["findings"] if CONFLICT in f["title"])
    # The arbiter gave no severity: the reviewer's stands, the dispute is on record.
    assert calls["arbiter"] == 1 and conflict["severity"] == "critical"
    assert "arbiter:severity" in conflict["trail"]


def test_arbiter_settles_a_severity_dispute(tmp_path):
    model, _ = fake_team(adjust=lowers("Затрагивает только текст сообщения."),
                         arbiter_severity="major")
    _, state, _, trace = run(tmp_path, lambda _id: model)
    conflict = next(f for f in state["findings"] if CONFLICT in f["title"])
    assert conflict["severity"] == "major" and "opponent:severity_disputed" in conflict["trail"]
    assert "say in severity_reason" not in trace


def test_report_summary_groups_critical_defects_by_the_task_kinds(tmp_path):
    model, _ = fake_team()
    _, _, report, _ = run(tmp_path, lambda _id: model)
    assert "- **Противоречивые:**" in report
    assert "непротиворечивость (ISO/IEC/IEEE 29148:2018, п. 5.2.6; IEEE Std 830-1998, п. 4.3.4)" \
        in report


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



# --- code's checks before the opponent; a one-sided conflict is not lost -----------------

COMPLETENESS = "Полнота и документ целиком"


def conflict_also_in_completeness(prompt):
    """The completeness cell reports the same conflict as the consistency cell."""
    answer = honest_set_review(prompt)
    if COMPLETENESS in prompt and not answer["findings"]:
        answer = honest_set_review(prompt.replace(COMPLETENESS, "Непротиворечивость набора"))
    return answer


def test_duplicates_are_merged_before_the_opponent_pays_for_them(tmp_path):
    seen = []
    model, _ = fake_team(set_review=conflict_also_in_completeness,
                         refute=lambda title: seen.append(title) or False)
    _, state, _, trace = run(tmp_path, lambda _id: model)
    assert sum(CONFLICT in t for t in seen) == 1  # the opponent judged it once
    assert '"prescreen_summary"' in trace and '"merged": 1' in trace
    assert sum(CONFLICT in f["title"] for f in state["findings"]) == 1


def one_sided_conflict(keep: list):
    def review(prompt):
        answer = honest_set_review(prompt)
        for f in answer["findings"]:
            if f["criterion"] == "set_consistent":
                keep.append(f["evidence"][1])
                f["evidence"] = f["evidence"][:1]
        return answer
    return review


def test_opponent_refiles_a_one_sided_conflict_as_a_gap(tmp_path):
    def refile(title):
        return ({"verdict": "adjust", "criterion": "set_complete"} if CONFLICT in title
                else None)

    model, _ = fake_team(set_review=one_sided_conflict([]), adjust=refile)
    _, state, _, trace = run(tmp_path, lambda _id: model)
    assert "file it under `complete`" in trace  # the reviewer was told how to fix it
    gap = next(f for f in state["findings"] if CONFLICT in f["title"])
    assert gap["criterion"] == "set_complete"
    assert not any("gate:one_sided" in f["trail"] for f in state["rejected"])


def test_opponent_can_quote_the_missing_side_of_a_conflict(tmp_path):
    second: list = []

    def supply(title):
        return {"other_side": second[:1]} if CONFLICT in title and second else None

    model, _ = fake_team(set_review=one_sided_conflict(second), adjust=supply)
    _, state, _, _ = run(tmp_path, lambda _id: model)
    conflict = next(f for f in state["findings"] if CONFLICT in f["title"])
    assert conflict["criterion"] == "set_consistent" and len(conflict["evidence"]) == 2
    assert "opponent:other_side" in conflict["trail"]


def test_one_sided_conflict_nobody_fixed_is_dropped_by_the_final_gate(tmp_path):
    model, _ = fake_team(set_review=one_sided_conflict([]))
    _, state, _, _ = run(tmp_path, lambda _id: model)
    assert any("gate:one_sided" in f["trail"] for f in state["rejected"])



def test_opponent_cannot_turn_a_gap_into_a_one_sided_conflict(tmp_path):
    """The reviewer filed a gap correctly; the opponent relabels it as a conflict without the
    other side. The relabelling is not applied, so the gate does not lose the defect."""
    def relabel(title):
        return ({"verdict": "adjust", "criterion": "set_consistent"} if "Непроверяемая" in title
                else None)

    model, _ = fake_team(adjust=relabel)
    _, state, _, trace = run(tmp_path, lambda _id: model)
    kept = [f for f in state["findings"] if "Непроверяемая" in f["title"]]
    assert kept and all(f["criterion"] == "verifiable" for f in kept)
    assert all("opponent:criterion_kept" in f["trail"] for f in kept)
    assert "you move it to set_consistent, but it quotes one statement" in trace



def test_a_defect_found_on_the_way_is_filed_under_its_own_criterion(tmp_path):
    """The consistency cell reports a verifiability defect: accepted, no retry."""
    def with_foreign(prompt):
        answer = honest_set_review(prompt)
        if "Непротиворечивость набора" in prompt and answer["findings"]:
            extra = dict(answer["findings"][0], criterion="verifiable",
                         title="Срок без единицы измерения", evidence=answer["findings"][0][
                             "evidence"][:1])
            answer["findings"].append(extra)
        return answer

    model, _ = fake_team(set_review=with_foreign)
    _, state, _, trace = run(tmp_path, lambda _id: model)
    assert "is not part of this check" not in trace and "not a known code" not in trace
    assert any(f["title"] == "Срок без единицы измерения" for f in state["findings"])



def test_a_checklist_gap_needs_a_finding(tmp_path):
    """Completeness enumerates first; a gap it lists but does not report goes back."""
    def lists_a_gap(prompt):
        answer = honest_set_review(prompt)
        if "Полнота и документ целиком" in prompt:
            block = re.search(r"^\[(b\d{4})\]", prompt, re.MULTILINE).group(1)
            answer["checklist"] = [{"item": "Отказ внешней системы", "block_id": block,
                                    "status": "gap", "note": "Не описано."}]
        return answer

    model, _ = fake_team(set_review=lists_a_gap)
    _, _, _, trace = run(tmp_path, lambda _id: model)
    assert "is marked gap, but no finding quotes block" in trace


def test_a_violation_named_in_reviewed_needs_its_finding(tmp_path):
    def names_but_skips(prompt):
        answer = honest_review(prompt)
        for r in answer["reviewed"]:
            r["violated"] = ["singular"]
        return answer

    model, _ = fake_team(review=names_but_skips)
    _, _, _, trace = run(tmp_path, lambda _id: model)
    assert "names `singular` as violated, but no finding under `singular`" in trace


def test_traceability_matrix_must_cover_every_main_requirement():
    from reqaudit.stages.s5_candidates import ChecklistAnswer, ChecklistItem, cell_check

    cell = Cell(id="parent_child", kind=CellKind.PARENT_CHILD, title="t", criteria=[])
    check = cell_check(cell, {"R-001", "R-002"}, {}, trace_units=["R-001", "R-002"])
    answer = ChecklistAnswer(analysis="ok", findings=[], checklist=[
        ChecklistItem(unit_id="R-001", item="Операция", block_id="b0001", status="ok",
                      note="Есть в API.")])
    complaint = check(answer) or ""
    assert "must trace every requirement of the main part" in complaint and "R-002" in complaint



def test_a_gap_with_something_still_missing_cannot_be_refuted_whole(tmp_path):
    def refute_but_admit(title):
        return ({"verdict": "refuted", "still_missing": "что делать при таймауте"}
                if "Непроверяемая" in title else None)

    def as_gap(prompt):
        answer = honest_review(prompt)
        for f in answer["findings"]:
            f["criterion"] = "complete"
        return answer

    model, _ = fake_team(review=as_gap, adjust=refute_but_admit)
    _, _, _, trace = run(tmp_path, lambda _id: model)
    assert "you refute the gap but write that" in trace


def test_same_places_quoted_under_two_criteria_are_one_defect():
    a = _f("D1", ["b1", "b2"], criterion="set_consistent", severity=Severity.CRITICAL)
    b = _f("D2", ["b1", "b2"], criterion="correct")
    single_a = _f("D3", ["b9"], criterion="unambiguous")
    single_b = _f("D4", ["b9"], criterion="singular")
    merged, n = merge_duplicates([a, b, single_a, single_b])
    assert n == 1 and len(merged) == 3  # the pair merged; one place, two criteria kept apart


def test_reviewer_opponent_and_arbiter_share_the_gap_rules(tmp_path):
    """The arbiter must judge a gap by the rules the opponent refuted it under."""
    from reqaudit.criteria import Criterion
    from reqaudit.stages.s5_candidates import _instructions

    cell = Cell(id="completeness", kind=CellKind.COMPLETENESS, title="t",
                criteria=[Criterion.COMPLETE])
    text, ids = _instructions(cell, has_parent_child=False)
    assert "What counts as a gap" in text and any(i.startswith("gaps@") for i in ids)
    source = Path("src/reqaudit/stages/s6_review.py").read_text(encoding="utf-8")
    assert source.count('"severity", "gaps"') == 2


def test_completeness_checklist_must_cover_every_limit_and_status():
    """Limits, periods and statuses the fact table found each need a checklist item; the model
    still judges each one, code only makes sure none is skipped."""
    from types import SimpleNamespace

    from reqaudit.criteria import Criterion
    from reqaudit.models import Fact, FactTable
    from reqaudit.stages.s5_candidates import (
        ChecklistAnswer,
        ChecklistItem,
        cell_check,
        coverage_anchors,
    )

    def fact(fid, entity, attribute, value, block):
        return Fact(id=fid, entity=entity, entity_label=entity, attribute=attribute,
                    value=value, normalized=value, block_id=block, quote=value)

    facts = FactTable(conflicts=[], facts=[
        fact("F-1", "draft_retention", "timing", "30", "b1"),
        fact("F-2", "draft_retention", "unit", "дней", "b1"),
        fact("F-3", "application_status", "status", "Одобрена", "b2"),
        fact("F-4", "application_status", "status", "Отозвана", "b3"),
        fact("F-5", "currency", "value", "тенге", "b4"),
    ])
    blocks = {b: SimpleNamespace(id=b, text="") for b in ("b1", "b2", "b3", "b4")}
    cell = Cell(id="completeness", kind=CellKind.COMPLETENESS, title="t",
                criteria=[Criterion.COMPLETE])
    anchors = coverage_anchors(SimpleNamespace(facts=facts), cell, blocks)
    assert sorted(a.blocks[0] for a in anchors) == ["b1", "b2", "b3"]  # no plain values

    check = cell_check(cell, set(), blocks, anchors=anchors)
    item = ChecklistItem(item="Срок хранения черновика", block_id="b1", status="ok",
                         note="Задан срок 30 дней.")
    complaint = check(ChecklistAnswer(analysis="Итог проверки.", checklist=[item], findings=[]))
    assert complaint and "Missing 2" in complaint and "b2" in complaint
