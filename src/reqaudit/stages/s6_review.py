"""s6 Opponent and arbiter: every candidate is attacked before it can reach the report.

The opponent sees the document and the finding, not the reviewer's reasoning, and tries to
refute it. Upheld findings are confirmed, adjusted ones are confirmed with the opponent's
correction, refuted ones go to the arbiter, who reads both sides and decides.
If the opponent is unavailable, findings stay but are marked as not challenged.

The opponent also tests the proposed fix by counterfactual: with the suggestion applied
to the document, is the defect gone, and does the new wording not contradict another
place? A fix that fails is replaced by the opponent's corrected one, so the report never
recommends an edit that leaves the defect or creates a new one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from reqaudit import prompt_store
from reqaudit.criteria import Criterion
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.models import CheckFailure, Finding, FindingStatus, Severity
from reqaudit.reading.render import LEGEND
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import (
    EvidenceDraft,
    document_view,
    evidence_problem,
    profile_view,
    requirement_index,
)

BATCH_SIZE = 8


class Verdict(BaseModel):
    ref: str
    verdict: Literal["upheld", "adjust", "refuted"]
    reasoning: str
    counter_evidence: list[EvidenceDraft] = []
    severity: Severity | None = None
    criterion: Criterion | None = None
    fix_works: bool = Field(description="With the suggestion applied, the defect is gone and "
                            "the new wording contradicts nothing else in the document.")
    better_fix: str = Field(default="", description="In Russian: a corrected suggestion, "
                            "required when fix_works is false.")

    @field_validator("verdict", mode="before")
    @classmethod
    def _unknown_verdict_is_refuted(cls, value: object) -> object:
        # An unclear verdict goes to the arbiter rather than straight into the report.
        return value if value in ("upheld", "adjust", "refuted") else "refuted"

    @field_validator("severity", "criterion", mode="before")
    @classmethod
    def _invalid_is_none(cls, value: object, info) -> object:
        allowed = ({s.value for s in Severity} if info.field_name == "severity"
                   else {c.value for c in Criterion})
        return value if value in allowed else None


class OpponentAnswer(BaseModel):
    verdicts: list[Verdict]


class Decision(BaseModel):
    ref: str
    keep: bool
    reasoning: str
    severity: Severity | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _invalid_is_none(cls, value: object) -> object:
        return value if value in {s.value for s in Severity} else None


class ArbiterAnswer(BaseModel):
    decisions: list[Decision]


class ReviewResult(BaseModel):
    """Artifact of s6."""

    confirmed: list[Finding]
    rejected: list[Finding]
    verdicts: dict[str, str]


def _finding_view(ref: str, f: Finding, with_fix: bool = False) -> str:
    quotes = "\n".join(f"  - [{e.block_id}] «{e.quote}»" for e in f.evidence)
    fix = f"\n  suggestion: {f.suggestion or '—'}" if with_fix else ""
    return (f"{ref}: criterion={f.criterion.value} severity={f.severity.value} "
            f"requirements={','.join(f.unit_ids) or '—'}\n"
            f"  title: {f.title}\n  explanation: {f.explanation}{fix}\n  evidence:\n{quotes}")


def _context(ctx: RunContext) -> str:
    state = ctx.state
    return "\n\n".join([document_view(state.document, {b.id for b in state.document.blocks},
                                      ctx.settings.window_chars),
                        profile_view(state.profile), requirement_index(state.units)])


def _batches(findings: list[Finding]) -> list[list[Finding]]:
    by_cell: dict[str, list[Finding]] = {}
    for f in findings:
        by_cell.setdefault(f.cell or "", []).append(f)
    return [group[i:i + BATCH_SIZE] for group in by_cell.values()
            for i in range(0, len(group), BATCH_SIZE)]


def _covers_refs(refs: list[str], field: str, blocks: dict) -> Check:
    def check(answer: OpponentAnswer | ArbiterAnswer) -> str | None:
        items = getattr(answer, field)
        missing = [r for r in refs if r not in {i.ref for i in items}]
        complaints = ([f"give a {field[:-1]} for every finding; missing {missing}"]
                      if missing else [])
        for item in items:
            for e in getattr(item, "counter_evidence", []):
                if problem := evidence_problem(e, blocks):
                    complaints.append(f"{item.ref}: counter_evidence {problem}")
            if (getattr(item, "fix_works", True) is False and item.verdict != "refuted"
                    and not item.better_fix.strip()):
                complaints.append(f"{item.ref}: fix_works is false, so give better_fix: the "
                                  "corrected suggestion")
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in complaints)) if complaints else None

    russian = [f"{field}.reasoning"] + (["verdicts.better_fix"] if field == "verdicts" else [])
    return combine(check, require_russian(*russian))


class ReviewStage(Stage):
    code = "s6"
    title = "Оппонент и арбитр"
    artifact = "s6_review.json"

    def run(self, ctx: RunContext) -> ReviewResult:
        state = ctx.state
        blocks = {b.id: b for b in state.document.blocks if not b.hidden}
        context = _context(ctx)
        batches = _batches(state.candidates)

        instructions, ids = prompt_store.compose("s6_opponent", legend=LEGEND)
        requests = []
        for n, batch in enumerate(batches, 1):
            refs = [f"F{i}" for i in range(1, len(batch) + 1)]
            listing = "\n\n".join(_finding_view(r, f, with_fix=True)
                                  for r, f in zip(refs, batch, strict=True))
            requests.append(dict(
                step=f"s6_opponent[{n}]", role="opponent", instructions=instructions,
                prompt=f"{context}\n\n<findings>\n{listing}\n</findings>\n",
                output_type=OpponentAnswer, prompt_ids=ids,
                check=_covers_refs(refs, "verdicts", blocks)))

        confirmed: list[Finding] = []
        disputed: list[tuple[Finding, Verdict | None]] = []
        verdicts: dict[str, str] = {}
        for batch, result in zip(batches, ctx.llm.many(requests), strict=True):
            if isinstance(result, LLMError):
                state.check_failures.append(CheckFailure(stage=self.code, cell=batch[0].cell or "",
                                                         message=result.message))
                for f in batch:
                    confirmed.append(f.model_copy(update={
                        "status": FindingStatus.CONFIRMED,
                        "trail": [*f.trail, "opponent:unavailable"],
                        "review_note": "Оппонент недоступен: находка не оспаривалась."}))
                    verdicts[f.id] = "unavailable"
                continue
            by_ref = {v.ref: v for v in result.verdicts}
            for i, f in enumerate(batch, 1):
                v = by_ref.get(f"F{i}")
                verdicts[f.id] = v.verdict if v else "missing"
                if v and not v.fix_works and v.better_fix.strip() and v.verdict != "refuted":
                    f = f.model_copy(update={"suggestion": v.better_fix.strip(),
                                             "trail": [*f.trail, "opponent:fix_rewritten"]})
                if v and v.verdict == "upheld":
                    confirmed.append(f.model_copy(update={
                        "status": FindingStatus.CONFIRMED, "trail": [*f.trail, "opponent:upheld"],
                        "review_note": v.reasoning}))
                elif v and v.verdict == "adjust":
                    confirmed.append(f.model_copy(update={
                        "status": FindingStatus.CONFIRMED,
                        "severity": v.severity or f.severity,
                        "criterion": v.criterion or f.criterion,
                        "trail": [*f.trail, "opponent:adjusted"], "review_note": v.reasoning}))
                else:
                    disputed.append((f, v))

        kept, rejected = self._arbitrate(ctx, context, disputed, blocks)
        confirmed.extend(kept)
        result = ReviewResult(confirmed=confirmed, rejected=rejected, verdicts=verdicts)
        state.findings = confirmed
        state.rejected = rejected
        ctx.trace.event("review_summary", stage=self.code, confirmed=len(confirmed),
                        rejected=len(rejected), disputed=len(disputed))
        return result

    def _arbitrate(self, ctx: RunContext, context: str,
                   disputed: list[tuple[Finding, Verdict | None]],
                   blocks: dict) -> tuple[list[Finding], list[Finding]]:
        if not disputed:
            return [], []
        instructions, ids = prompt_store.compose("s6_arbiter", legend=LEGEND)
        chunks = [disputed[i:i + BATCH_SIZE] for i in range(0, len(disputed), BATCH_SIZE)]
        requests = []
        for n, chunk in enumerate(chunks, 1):
            refs = [f"D{i}" for i in range(1, len(chunk) + 1)]
            items = []
            for ref, (f, v) in zip(refs, chunk, strict=True):
                counter = "\n".join(f"  - [{e.block_id}] «{e.quote}»"
                                    for e in (v.counter_evidence if v else []))
                opponent = v.reasoning if v else "Оппонент не дал заключения."
                items.append(_finding_view(ref, f) + f"\n  opponent: {opponent}"
                             + (f"\n  opponent's quotes:\n{counter}" if counter else ""))
            requests.append(dict(
                step=f"s6_arbiter[{n}]", role="judge", instructions=instructions,
                prompt=f"{context}\n\n<disputes>\n" + "\n\n".join(items) + "\n</disputes>\n",
                output_type=ArbiterAnswer, prompt_ids=ids,
                check=_covers_refs(refs, "decisions", blocks)))

        kept: list[Finding] = []
        rejected: list[Finding] = []
        for chunk, result in zip(chunks, ctx.llm.many(requests), strict=True):
            by_ref = {} if isinstance(result, LLMError) else {d.ref: d for d in result.decisions}
            if isinstance(result, LLMError):
                ctx.state.check_failures.append(CheckFailure(
                    stage=self.code, cell="арбитр", message=result.message))
            for i, (f, v) in enumerate(chunk, 1):
                d = by_ref.get(f"D{i}")
                objection = v.reasoning if v else "оппонент не дал заключения"
                if d is None:
                    # No arbiter decision: a disputed finding does not enter the report.
                    rejected.append(f.model_copy(update={
                        "status": FindingStatus.REJECTED, "trail": [*f.trail, "arbiter:none"],
                        "review_note": f"Оппонент: {objection}. Арбитр не вынес решения."}))
                elif d.keep:
                    kept.append(f.model_copy(update={
                        "status": FindingStatus.CONFIRMED, "severity": d.severity or f.severity,
                        "trail": [*f.trail, "opponent:refuted", "arbiter:kept"],
                        "review_note": f"Оппонент: {objection} Арбитр: {d.reasoning}"}))
                else:
                    rejected.append(f.model_copy(update={
                        "status": FindingStatus.REJECTED,
                        "trail": [*f.trail, "opponent:refuted", "arbiter:dropped"],
                        "review_note": f"Оппонент: {objection} Арбитр: {d.reasoning}"}))
        return kept, rejected
