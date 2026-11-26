"""s5 Candidates: one reviewer agent per cell looks for defects.

Cells run in parallel. Each answer is checked by code before it is accepted: criteria
belong to the cell, requirement ids exist, every quote is in its block, conflicts quote
both sides, every requirement of a requirements cell was reviewed. Complaints go back to
the model; what is still wrong after the last retry is dropped later by the gates (s7).
A cell the model could not do is recorded as unchecked, never silently skipped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from reqaudit import criteria, prompt_store
from reqaudit.criteria import Criterion
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.markers import MarkerHit, find_markers
from reqaudit.models import (
    Cell,
    CellKind,
    CheckFailure,
    Evidence,
    Finding,
    FindingStatus,
    MarkerRecord,
    scope_of,
)
from reqaudit.reading.render import LEGEND, needed_blocks
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import (
    CandidateDraft,
    document_view,
    evidence_problem,
    fact_table_view,
    profile_view,
    requirement_index,
)

ROLE = "judge"
_ANOTHER_PASS = ("\nAnother reviewer has checked this scope already. Work independently and "
                 "in the opposite order: start from the last requirement, or from the end of "
                 "the document, and move backwards.\n")
_MAX_COMPLAINTS = 25
_RUSSIAN_FIELDS = ("findings.title", "findings.explanation", "findings.suggestion",
                   "findings.question", "reviewed.note", "analysis")


class Reviewed(BaseModel):
    unit_id: str
    violated: list[str] = []  # criterion codes this requirement violates; empty when sound
    note: str


class RequirementsAnswer(BaseModel):
    reviewed: list[Reviewed]
    findings: list[CandidateDraft]


class MarkerVerdict(BaseModel):
    marker_id: str
    defect: bool
    reason: str


class RequirementsWithMarkersAnswer(BaseModel):
    """Used only for cells where code found marker words; other cells keep the plain schema,
    so their requests stay exactly as before."""

    reviewed: list[Reviewed]
    markers: list[MarkerVerdict]
    findings: list[CandidateDraft]


class SetAnswer(BaseModel):
    analysis: str
    findings: list[CandidateDraft]


class ChecklistItem(BaseModel):
    unit_id: str | None = None  # the requirement the item traces, when it is one
    item: str  # what is checked, in Russian: an operation, a rule, a system and its failure
    block_id: str  # where the document states the thing the item is about
    status: Literal["ok", "gap", "conflict", "n/a"]
    note: str  # in Russian: where it is covered, or what is missing


class ChecklistAnswer(BaseModel):
    """For checks whose misses come from skipping items: enumerate first, then judge. Code
    holds the two together: an item marked gap or conflict needs a finding quoting it."""

    checklist: list[ChecklistItem]
    analysis: str
    findings: list[CandidateDraft]


# Checks whose defects are things missing from a list the document itself implies.
_CHECKLIST_KINDS = {CellKind.PARENT_CHILD, CellKind.COMPLETENESS, CellKind.ACCEPTANCE}


class CandidateSet(BaseModel):
    """Artifact of s5."""

    candidates: list[Finding]
    failures: list[CheckFailure]
    dropped_unknown_criterion: int = 0


def _answer_type(cell: Cell, markers: list[MarkerHit]) -> type[BaseModel]:
    if cell.kind in _CHECKLIST_KINDS:
        return ChecklistAnswer
    if cell.kind != CellKind.REQUIREMENTS:
        return SetAnswer
    return RequirementsWithMarkersAnswer if markers else RequirementsAnswer


def cell_check(cell: Cell, unit_ids: set[str], blocks: dict,
               markers: list[MarkerHit] | None = None,
               trace_units: list[str] | None = None) -> Check:
    # A cell names the criteria it is about, but a defect found on the way is filed under the
    # criterion that fits it, whichever that is: duplicates across cells are merged before
    # the opponent, and sending the answer back for a «foreign» criterion only cost a call.
    known = {c.value for c in Criterion}
    markers = markers or []

    def check(answer: RequirementsAnswer | RequirementsWithMarkersAnswer | SetAnswer
              ) -> str | None:
        complaints: list[str] = []
        if hasattr(answer, "reviewed"):
            missing = [u for u in cell.unit_ids if u not in {r.unit_id for r in answer.reviewed}]
            if missing:
                complaints.append(f"`reviewed` must list every requirement; missing {missing}")
            reported = {(u, str(f.criterion)) for f in answer.findings for u in f.unit_ids}
            for r in answer.reviewed:
                for code in r.violated:
                    if code in known and (r.unit_id, code) not in reported:
                        complaints.append(
                            f"reviewed {r.unit_id} names `{code}` as violated, but no finding "
                            f"under `{code}` names {r.unit_id}: report it, or remove it from "
                            "`violated` if the requirement meets the criterion")
        if isinstance(answer, ChecklistAnswer):
            complaints += _checklist_complaints(answer, blocks)
            if trace_units:
                listed = {i.unit_id for i in answer.checklist}
                missing = [u for u in trace_units if u not in listed]
                if missing:
                    complaints.append(
                        "`checklist` must trace every requirement of the main part, one item "
                        f"each with its `unit_id` (status n/a when the interface has nothing to "
                        f"do with it); missing {missing}")
        if isinstance(answer, RequirementsWithMarkersAnswer):
            decided = {m.marker_id: m for m in answer.markers}
            undecided = [m.id for m in markers if m.id not in decided]
            if undecided:
                complaints.append(f"`markers` must decide on every marker; missing {undecided}")
            flagged = {m.unit_id for m in markers if decided.get(m.id) and decided[m.id].defect}
            reported = {u for f in answer.findings for u in f.unit_ids}
            for unit in sorted(flagged - reported):
                complaints.append(f"a marker in {unit} was judged a defect, but no finding "
                                  "names that requirement; report it or change the decision")
        for n, f in enumerate(answer.findings):
            where = f"findings[{n}] ({f.title[:40]!r})"
            if str(f.criterion) not in known:
                complaints.append(f"{where}: criterion {f.criterion!r} is not a known code; "
                                  f"use one of {sorted(known)}")
            unknown = [u for u in f.unit_ids if u not in unit_ids]
            if unknown:
                complaints.append(f"{where}: unknown requirement ids {unknown}")
            if not f.evidence:
                complaints.append(f"{where}: evidence is empty; quote the text that shows it")
            for e in f.evidence:
                if problem := evidence_problem(e, blocks):
                    complaints.append(f"{where}: {problem}")
            if f.criterion == Criterion.SET_CONSISTENT and len(f.evidence) < 2:
                complaints.append(
                    f"{where}: a conflict needs a quote from each side. If the document does "
                    "state the other side, quote it too. If it does not, this is a gap, not a "
                    "conflict: file it under `complete` (one requirement lacks something) or "
                    "`set_complete` (the set lacks a rule, case or operation)")
        if not complaints:
            return None
        shown = complaints[:_MAX_COMPLAINTS]
        more = len(complaints) - len(shown)
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in shown)
                + (f"\n- and {more} more of the same kind" if more > 0 else ""))

    return combine(check, require_russian(*_RUSSIAN_FIELDS, "markers.reason",
                                          "checklist.note"))


def _main_units(state, cell: Cell) -> list[str]:
    """For the parent/child check: every requirement of the parts that are not the child."""
    if cell.kind != CellKind.PARENT_CHILD or state.profile is None:
        return []
    children = {p.name for p in state.profile.parts if p.role.value == "child_spec"}
    return [u.id for u in state.units if u.part not in children
            and u.kind.value not in ("acceptance_criterion", "api_contract")]


def _checklist_complaints(answer: ChecklistAnswer, blocks: dict) -> list[str]:
    out = []
    if not answer.checklist:
        out.append("`checklist` is empty: list every item this check covers before judging")
    quoted = {e.block_id for f in answer.findings for e in f.evidence}
    for n, item in enumerate(answer.checklist):
        if item.block_id not in blocks:
            out.append(f"checklist[{n}] ({item.item[:40]!r}): block {item.block_id} does not "
                       "exist; give the block that states the thing checked")
        elif item.status in ("gap", "conflict") and item.block_id not in quoted:
            out.append(f"checklist[{n}] ({item.item[:40]!r}) is marked {item.status}, but no "
                       f"finding quotes block {item.block_id}: report it, or change the status "
                       "if the document covers it")
    return out


def _instructions(cell: Cell, has_parent_child: bool,
                  with_markers: bool = False) -> tuple[str, list[str]]:
    scope_note = ("Conflicts between the main part and its child specification are covered by a "
                  "separate check; report here conflicts within one part and between "
                  "requirements and acceptance criteria." if has_parent_child else "")
    criteria_text = criteria.cards(list(cell.criteria))
    names = ["s5_common", "severity", f"s5_{cell.kind.value}"] + (
        ["s5_markers"] if with_markers else [])
    return prompt_store.compose(*names, legend=LEGEND, criteria=criteria_text,
                                scope_note=scope_note)


def _outside_view(state) -> str:
    """Visible text blocks that no requirement covers; the general cell reads them first."""
    covered = {b for u in state.units for b in u.block_ids}
    needed = set(needed_blocks(state.document.blocks))
    unclassified = set(state.coverage.unclassified) if state.coverage else set()
    obligations = state.coverage.obligations_outside if state.coverage else {}

    def line(block_id: str) -> str:
        notes = (["не классифицирован"] if block_id in unclassified else []) + (
            [f"обязательство: «{obligations[block_id]}»"] if block_id in obligations else [])
        return block_id + (f" ({'; '.join(notes)})" if notes else "")

    outside = [b.id for b in state.document.blocks if b.id in needed and b.id not in covered]
    # Blocks the second segmenter flagged come first: they are the likeliest misses.
    outside.sort(key=lambda i: i not in obligations)
    lines = [line(i) for i in outside]
    listed = "\n".join(lines) or "нет"
    return f"<blocks_outside_requirements>\n{listed}\n</blocks_outside_requirements>"


def _prompt(ctx: RunContext, cell: Cell, markers: list[MarkerHit] | None = None) -> str:
    state = ctx.state
    units = {u.id: u for u in state.units}
    focus = {b for uid in cell.unit_ids for b in units[uid].block_ids} | set(cell.block_ids)
    parts = [document_view(state.document, focus or {b.id for b in state.document.blocks},
                           ctx.settings.window_chars),
             profile_view(state.profile),
             requirement_index(state.units)]
    if cell.kind == CellKind.VALUES and state.facts is not None:
        parts.append(fact_table_view(state.facts))
    if cell.kind == CellKind.GENERAL:
        parts.append(_outside_view(state))
    if cell.kind == CellKind.FOCUS:
        parts.append(f"<focus>\n{cell.instruction}\n</focus>")
    if cell.kind == CellKind.REQUIREMENTS:
        listed = "\n".join(
            f"{u.id} ({u.kind.value}, {u.doc_label or 'без номера'}, blocks "
            f"{', '.join(u.block_ids)}): «{u.quote}»" for u in (units[i] for i in cell.unit_ids))
        parts.append(f"<requirements_to_check>\n{listed}\n</requirements_to_check>")
        if markers:
            found = "\n".join(f"{m.id} in {m.unit_id}: «{m.word}» ({m.category_ru})"
                              for m in markers)
            parts.append(f"<marker_candidates>\n{found}\n</marker_candidates>")
    parts.append(f"Check: {cell.title}.")
    return "\n\n".join(parts) + "\n"


def _to_findings(cell: Cell, answer: RequirementsAnswer | SetAnswer,
                 review_pass: int = 1) -> tuple[list[Finding], int]:
    """Candidates from one cell, and how many were dropped for an unknown criterion."""
    findings = []
    dropped = 0
    for n, draft in enumerate(answer.findings, 1):
        # `Criterion | str` keeps a plain string as str, so convert explicitly.
        try:
            criterion = Criterion(draft.criterion)
        except ValueError:
            dropped += 1
            continue
        findings.append(Finding(
            id=f"{cell.id}#{n}" if review_pass == 1 else f"{cell.id}/{review_pass}#{n}",
            scope=scope_of(criterion, list(draft.unit_ids)),
            criterion=criterion,
            unit_ids=list(draft.unit_ids),
            title=draft.title.strip(),
            explanation=draft.explanation.strip(),
            evidence=[Evidence(block_id=e.block_id, quote=e.quote) for e in draft.evidence],
            severity=draft.severity,
            suggestion=draft.suggestion.strip() or None,
            status=FindingStatus.CANDIDATE,
            trail=["reviewer"],
            question=(draft.question or "").strip() or None,
            question_to=draft.question_to,
            cell=cell.id,
        ))
    return findings, dropped


class CandidatesStage(Stage):
    code = "s5"
    title = "Кандидаты в дефекты"
    artifact = "s5_candidates.json"

    def run(self, ctx: RunContext) -> CandidateSet:
        state = ctx.state
        assert state.document is not None and state.profile is not None, "s5 needs s1 and s2"
        blocks = {b.id: b for b in state.document.blocks if not b.hidden}
        unit_ids = {u.id for u in state.units}
        has_parent_child = any(c.kind == CellKind.PARENT_CHILD for c in state.cells)

        hits = find_markers(state.units) if ctx.settings.markers else []
        # Every cell is reviewed by independent passes and their findings are united: one
        # pass of a model finds a different subset of the defects each time, and the union
        # is both fuller and steadier. Duplicates are merged and false ones refuted in s6.
        passes = max(1, ctx.settings.review_passes)
        requests, owners = [], []
        for cell in state.cells:
            # Cells without marker hits keep exactly the request they had before markers.
            mine = ([h for h in hits if h.unit_id in set(cell.unit_ids)]
                    if cell.kind == CellKind.REQUIREMENTS else [])
            instructions, ids = _instructions(cell, has_parent_child, with_markers=bool(mine))
            prompt = _prompt(ctx, cell, mine)
            for n in range(1, passes + 1):
                requests.append(dict(
                    step=f"s5[{cell.id}]" if n == 1 else f"s5[{cell.id}/{n}]",
                    role="reviewer" if cell.kind == CellKind.REQUIREMENTS else ROLE,
                    instructions=instructions,
                    prompt=prompt if n == 1 else prompt + _ANOTHER_PASS,
                    output_type=_answer_type(cell, mine),
                    prompt_ids=ids, check=cell_check(cell, unit_ids, blocks, mine,
                                                     _main_units(state, cell))))
                owners.append((cell, n))

        candidates: list[Finding] = []
        failures: list[CheckFailure] = []
        unknown = 0
        verdicts: dict[str, MarkerVerdict] = {}
        answered: set[str] = set()
        errors: dict[str, LLMError] = {}
        for (cell, n), result in zip(owners, ctx.llm.many(requests), strict=True):
            if isinstance(result, LLMError):
                errors.setdefault(cell.id, result)
                continue
            answered.add(cell.id)
            found, dropped = _to_findings(cell, result, n)
            candidates.extend(found)
            unknown += dropped
            for verdict in getattr(result, "markers", []):
                if verdict.marker_id not in verdicts or verdict.defect:
                    verdicts[verdict.marker_id] = verdict
        for cell in state.cells:  # a cell is unchecked only when every pass failed
            if cell.id not in answered and cell.id in errors:
                failures.append(CheckFailure(stage=self.code, cell=cell.title,
                                             message=errors[cell.id].message))

        state.candidates = candidates
        state.check_failures.extend(failures)
        state.markers = [MarkerRecord(
            id=h.id, unit_id=h.unit_id, category=h.category_ru, word=h.word,
            defect=verdicts[h.id].defect if h.id in verdicts else None,
            reason=verdicts[h.id].reason if h.id in verdicts else None) for h in hits]
        ctx.trace.event("candidates_summary", stage=self.code, candidates=len(candidates),
                        dropped_unknown_criterion=unknown,
                        failed_cells=[f.cell for f in failures])
        if state.cells and len(failures) == len(state.cells):
            raise LLMError("Ни одна проверка не выполнена: " + failures[0].message)
        return CandidateSet(candidates=candidates, failures=failures,
                            dropped_unknown_criterion=unknown)
