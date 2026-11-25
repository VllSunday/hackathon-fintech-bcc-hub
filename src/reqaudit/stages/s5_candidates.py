"""s5 Candidates: one reviewer agent per cell looks for defects.

Cells run in parallel. Each answer is checked by code before it is accepted: criteria
belong to the cell, requirement ids exist, every quote is in its block, conflicts quote
both sides, every requirement of a requirements cell was reviewed. Complaints go back to
the model; what is still wrong after the last retry is dropped later by the gates (s7).
A cell the model could not do is recorded as unchecked, never silently skipped.
"""

from __future__ import annotations

from pydantic import BaseModel

from reqaudit import criteria, prompt_store
from reqaudit.criteria import Criterion
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.models import (
    Cell,
    CellKind,
    CheckFailure,
    Evidence,
    Finding,
    FindingStatus,
    Scope,
)
from reqaudit.reading.render import LEGEND
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import (
    CandidateDraft,
    document_view,
    evidence_problem,
    profile_view,
    requirement_index,
)
from reqaudit.stages.s3_facts import fact_table_view

ROLE = "judge"
_MAX_COMPLAINTS = 25
_RUSSIAN_FIELDS = ("findings.title", "findings.explanation", "findings.suggestion",
                   "findings.question", "reviewed.note", "analysis")


class Reviewed(BaseModel):
    unit_id: str
    note: str


class RequirementsAnswer(BaseModel):
    reviewed: list[Reviewed]
    findings: list[CandidateDraft]


class SetAnswer(BaseModel):
    analysis: str
    findings: list[CandidateDraft]


class CandidateSet(BaseModel):
    """Artifact of s5."""

    candidates: list[Finding]
    failures: list[CheckFailure]
    dropped_unknown_criterion: int = 0


def _answer_type(cell: Cell) -> type[BaseModel]:
    return RequirementsAnswer if cell.kind == CellKind.REQUIREMENTS else SetAnswer


def cell_check(cell: Cell, unit_ids: set[str], blocks: dict) -> Check:
    allowed = set(cell.criteria)

    def check(answer: RequirementsAnswer | SetAnswer) -> str | None:
        complaints: list[str] = []
        if isinstance(answer, RequirementsAnswer):
            missing = [u for u in cell.unit_ids if u not in {r.unit_id for r in answer.reviewed}]
            if missing:
                complaints.append(f"`reviewed` must list every requirement; missing {missing}")
        for n, f in enumerate(answer.findings):
            where = f"findings[{n}] ({f.title[:40]!r})"
            if f.criterion not in allowed:
                complaints.append(f"{where}: criterion {f.criterion!r} is not part of this check; "
                                  f"use one of {sorted(c.value for c in allowed)}")
            unknown = [u for u in f.unit_ids if u not in unit_ids]
            if unknown:
                complaints.append(f"{where}: unknown requirement ids {unknown}")
            if not f.evidence:
                complaints.append(f"{where}: evidence is empty; quote the text that shows it")
            for e in f.evidence:
                if problem := evidence_problem(e, blocks):
                    complaints.append(f"{where}: {problem}")
            if f.criterion == Criterion.SET_CONSISTENT and len(f.evidence) < 2:
                complaints.append(f"{where}: a conflict needs a quote from each side")
        if not complaints:
            return None
        shown = complaints[:_MAX_COMPLAINTS]
        more = len(complaints) - len(shown)
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in shown)
                + (f"\n- and {more} more of the same kind" if more > 0 else ""))

    return combine(check, require_russian(*_RUSSIAN_FIELDS))


def _instructions(cell: Cell, has_parent_child: bool) -> tuple[str, list[str]]:
    scope_note = ("Conflicts between the main part and its child specification are covered by a "
                  "separate check; report here conflicts within one part and between "
                  "requirements and acceptance criteria." if has_parent_child else "")
    criteria_text = "\n".join(f"- `{c.value}`: {criteria.CRITERIA[c].definition}"
                              for c in cell.criteria)
    return prompt_store.compose("s5_common", f"s5_{cell.kind.value}", legend=LEGEND,
                                criteria=criteria_text, scope_note=scope_note)


def _prompt(ctx: RunContext, cell: Cell) -> str:
    state = ctx.state
    units = {u.id: u for u in state.units}
    focus = {b for uid in cell.unit_ids for b in units[uid].block_ids}
    parts = [document_view(state.document, focus or {b.id for b in state.document.blocks},
                           ctx.settings.window_chars),
             profile_view(state.profile),
             requirement_index(state.units)]
    if cell.kind == CellKind.VALUES and state.facts is not None:
        parts.append(fact_table_view(state.facts))
    if cell.kind == CellKind.REQUIREMENTS:
        listed = "\n".join(
            f"{u.id} ({u.kind.value}, {u.doc_label or 'без номера'}, blocks "
            f"{', '.join(u.block_ids)}): «{u.quote}»" for u in (units[i] for i in cell.unit_ids))
        parts.append(f"<requirements_to_check>\n{listed}\n</requirements_to_check>")
    parts.append(f"Check: {cell.title}.")
    return "\n\n".join(parts) + "\n"


def _to_findings(cell: Cell, answer: RequirementsAnswer | SetAnswer) -> tuple[list[Finding], int]:
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
        level = criteria.CRITERIA[criterion].level.value
        findings.append(Finding(
            id=f"{cell.id}#{n}",
            scope=Scope.REQUIREMENT if level == "requirement" else Scope.SET,
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

        requests = []
        for cell in state.cells:
            instructions, ids = _instructions(cell, has_parent_child)
            requests.append(dict(
                step=f"s5[{cell.id}]", role=ROLE, instructions=instructions,
                prompt=_prompt(ctx, cell), output_type=_answer_type(cell), prompt_ids=ids,
                check=cell_check(cell, unit_ids, blocks)))

        candidates: list[Finding] = []
        failures: list[CheckFailure] = []
        unknown = 0
        for cell, result in zip(state.cells, ctx.llm.many(requests), strict=True):
            if isinstance(result, LLMError):
                failures.append(CheckFailure(stage=self.code, cell=cell.title,
                                             message=result.message))
            else:
                found, dropped = _to_findings(cell, result)
                candidates.extend(found)
                unknown += dropped

        state.candidates = candidates
        state.check_failures.extend(failures)
        ctx.trace.event("candidates_summary", stage=self.code, candidates=len(candidates),
                        dropped_unknown_criterion=unknown,
                        failed_cells=[f.cell for f in failures])
        if state.cells and len(failures) == len(state.cells):
            raise LLMError("Ни одна проверка не выполнена: " + failures[0].message)
        return CandidateSet(candidates=candidates, failures=failures,
                            dropped_unknown_criterion=unknown)
