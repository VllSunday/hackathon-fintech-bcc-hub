"""s6 Opponent and arbiter: every candidate is attacked before it can reach the report.

The opponent sees the document and the finding, not the reviewer's reasoning, and tries to
refute it. Upheld findings are confirmed, adjusted ones are confirmed with the opponent's
correction, refuted ones go to the arbiter, who reads both sides and decides.
If the opponent is unavailable, findings stay but are marked as not challenged.

Severity follows one scale (prompts/severity.md) for every role. The opponent may change
a severity only with a reason; lowering a critical finding is a dispute the arbiter
decides, since an unchecked downgrade would quietly turn «do not start» into «start».

The opponent also tests the proposed fix by counterfactual: with the suggestion applied
to the document, is the defect gone, and does the new wording not contradict another
place? A fix that fails is replaced by the opponent's corrected one, so the report never
recommends an edit that leaves the defect or creates a new one.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, Field, field_validator

from reqaudit import criteria, prompt_store
from reqaudit.criteria import Criterion
from reqaudit.evidence import match_key
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.models import CheckFailure, Evidence, Finding, FindingStatus, Severity
from reqaudit.reading.render import LEGEND
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import (
    EvidenceDraft,
    document_view,
    evidence_problem,
    merge_duplicates,
    one_sided,
    profile_view,
    requirement_index,
    screen,
)
from reqaudit.stages.s6_calibrate import calibrate

BATCH_SIZE = 8


class Verdict(BaseModel):
    ref: str
    verdict: Literal["upheld", "adjust", "refuted"]
    reasoning: str
    counter_evidence: list[EvidenceDraft] = []
    other_side: list[EvidenceDraft] = Field(
        default_factory=list, description="For a conflict quoted from one side only: the "
        "statement that forms the other side, if the document has it.")
    severity: Severity | None = None
    criterion: Criterion | None = None
    still_missing: str = Field(default="", description="For a gap (complete, set_complete): "
                               "in Russian, what the document still does not answer about the "
                               "situation the gap names; empty only if it answers all of it.")
    severity_reason: str = Field(default="", description="In Russian: which point of the "
                                 "severity scale makes the new severity right; required when "
                                 "severity differs from the reviewer's.")
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
    note = ("\n  note: this conflict quotes only one side. Quote the other side in "
            "`other_side` if the document states it; if it does not, the defect is a gap: "
            "`adjust` it to `complete` or `set_complete`." if with_fix and one_sided(f) else "")
    return (f"{ref}: criterion={f.criterion.value} severity={f.severity.value} "
            f"requirements={','.join(f.unit_ids) or '—'}\n"
            f"  title: {f.title}\n  explanation: {f.explanation}{fix}{note}\n"
            f"  evidence:\n{quotes}")


def _context(ctx: RunContext) -> str:
    state = ctx.state
    return "\n\n".join([document_view(state.document, {b.id for b in state.document.blocks},
                                      ctx.settings.window_chars),
                        profile_view(state.profile), requirement_index(state.units)])


def _batches(findings: list[Finding]) -> list[list[Finding]]:
    """Batches of up to BATCH_SIZE, findings of one cell kept next to each other.

    Packed across cells: each batch carries the whole document as context, so a half-empty
    batch per cell pays for the document again without judging more findings.
    """
    by_cell: dict[str, list[Finding]] = {}
    for f in findings:
        by_cell.setdefault(f.cell or "", []).append(f)
    ordered = [f for group in by_cell.values() for f in group]
    return [ordered[i:i + BATCH_SIZE] for i in range(0, len(ordered), BATCH_SIZE)]


def _sides(f: Finding) -> int:
    return len({(e.block_id, match_key(e.quote)) for e in f.evidence})


def _criterion_after(f: Finding, v: Verdict) -> Criterion:
    """The opponent's criterion, unless it would turn a one-quote finding into a «conflict»
    the gate must drop: a relabelling must not invalidate a defect the opponent upheld."""
    if v.criterion == Criterion.SET_CONSISTENT and _sides(f) < 2:
        return f.criterion
    return v.criterion or f.criterion


def _covers_refs(refs: list[str], field: str, blocks: dict,
                 findings: dict[str, Finding] | None = None) -> Check:
    findings = findings or {}

    def check(answer: OpponentAnswer | ArbiterAnswer) -> str | None:
        items = getattr(answer, field)
        missing = [r for r in refs if r not in {i.ref for i in items}]
        complaints = ([f"give a {field[:-1]} for every finding; missing {missing}"]
                      if missing else [])
        for item in items:
            for e in getattr(item, "counter_evidence", []):
                if problem := evidence_problem(e, blocks):
                    complaints.append(f"{item.ref}: counter_evidence {problem}")
            for e in getattr(item, "other_side", []):
                if problem := evidence_problem(e, blocks):
                    complaints.append(f"{item.ref}: other_side {problem}")
            complaints += _severity_complaints(item, findings.get(item.ref))
            if (getattr(item, "fix_works", True) is False and item.verdict != "refuted"
                    and not item.better_fix.strip()):
                complaints.append(f"{item.ref}: fix_works is false, so give better_fix: the "
                                  "corrected suggestion")
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in complaints)) if complaints else None

    russian = [f"{field}.reasoning"] + (["verdicts.better_fix", "verdicts.still_missing"]
                                        if field == "verdicts" else [])
    return combine(check, require_russian(*russian))


_GAPS = (Criterion.COMPLETE, Criterion.SET_COMPLETE)


def _severity_complaints(item: Verdict | Decision, f: Finding | None) -> list[str]:
    """A severity change must name its reason on the scale; a contradiction the opponent
    itself confirmed is never minor; a gap with something still missing is narrowed, not
    refuted. Soft: the model may keep its answer on the last try."""
    if f is None or not isinstance(item, Verdict):
        return []
    if item.verdict == "refuted":
        if f.criterion in _GAPS and item.still_missing.strip():
            return [f"{item.ref}: you refute the gap but write that «{item.still_missing[:80]}» "
                    "is still not answered; then the gap stands: `adjust` it to what is still "
                    "missing instead of refuting it"]
        return []
    out = []
    if item.severity and item.severity != f.severity and not item.severity_reason.strip():
        out.append(f"{item.ref}: you changed severity {f.severity.value} → "
                   f"{item.severity.value}; say in severity_reason which point of the severity "
                   "scale makes it right")
    criterion = item.criterion or f.criterion
    if (item.criterion == Criterion.SET_CONSISTENT and f.criterion != Criterion.SET_CONSISTENT
            and not item.other_side and _sides(f) < 2):
        out.append(f"{item.ref}: you move it to set_consistent, but it quotes one statement; a "
                   "conflict needs both: quote the other one in other_side, or keep the "
                   "reviewer's criterion")
    if criterion == Criterion.SET_CONSISTENT and item.severity == Severity.MINOR:
        out.append(f"{item.ref}: you confirm a contradiction but rate it minor; by the scale a "
                   "contradiction is critical when it concerns behaviour and major when it is "
                   "confined to wording")
    return out


def _criteria_view(findings: list[Finding]) -> str:
    """The criteria the batch uses, with their boundaries: the opponent judges the fit."""
    codes = list(dict.fromkeys(f.criterion for f in findings))
    return "<criteria>\n" + criteria.brief(codes) + "\n</criteria>"


class ReviewStage(Stage):
    code = "s6"
    title = "Оппонент и арбитр"
    artifact = "s6_review.json"

    def run(self, ctx: RunContext) -> ReviewResult:
        state = ctx.state
        blocks = {b.id: b for b in state.document.blocks if not b.hidden}
        context = _context(ctx)
        # Code's checks first: the opponent is not paid for unfound quotes and duplicates.
        screened = screen(state.candidates, blocks, state.units, drop_one_sided=False)
        candidates, merged = merge_duplicates(screened.passed)
        ctx.trace.event("prescreen_summary", stage=self.code, candidates=len(state.candidates),
                        no_evidence=screened.stats["no_evidence"], merged=merged,
                        to_opponent=len(candidates),
                        one_sided=sum(1 for f in candidates if one_sided(f)))
        batches = _batches(candidates)

        instructions, ids = prompt_store.compose("s6_opponent", "severity", "gaps", legend=LEGEND)
        requests = []
        for n, batch in enumerate(batches, 1):
            refs = [f"F{i}" for i in range(1, len(batch) + 1)]
            listing = "\n\n".join(_finding_view(r, f, with_fix=True)
                                  for r, f in zip(refs, batch, strict=True))
            requests.append(dict(
                step=f"s6_opponent[{n}]", role="opponent", instructions=instructions,
                prompt=(f"{context}\n\n{_criteria_view(batch)}\n\n"
                        f"<findings>\n{listing}\n</findings>\n"),
                output_type=OpponentAnswer, prompt_ids=ids,
                check=_covers_refs(refs, "verdicts", blocks, dict(zip(refs, batch, strict=True)))))

        confirmed: list[Finding] = []
        disputed: list[Dispute] = []
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
                if v and v.other_side and v.verdict != "refuted":
                    f = f.model_copy(update={
                        "evidence": [*f.evidence, *(Evidence(block_id=e.block_id, quote=e.quote)
                                                    for e in v.other_side)],
                        "trail": [*f.trail, "opponent:other_side"]})
                if v and v.verdict == "upheld":
                    confirmed.append(f.model_copy(update={
                        "status": FindingStatus.CONFIRMED, "trail": [*f.trail, "opponent:upheld"],
                        "review_note": v.reasoning}))
                elif v and v.verdict == "adjust" and _lowers_critical(f, v):
                    disputed.append(Dispute(f, v, severity_only=True))
                elif v and v.verdict == "adjust":
                    criterion = _criterion_after(f, v)
                    kept_own = v.criterion is not None and criterion != v.criterion
                    confirmed.append(f.model_copy(update={
                        "status": FindingStatus.CONFIRMED,
                        "severity": v.severity or f.severity,
                        "criterion": criterion,
                        "trail": [*f.trail, "opponent:adjusted"]
                        + (["opponent:criterion_kept"] if kept_own else []),
                        "review_note": v.reasoning}))
                else:
                    disputed.append(Dispute(f, v))

        kept, rejected = self._arbitrate(ctx, context, disputed, blocks)
        confirmed.extend(kept)
        confirmed = calibrate(ctx, context, confirmed)
        rejected = [*screened.rejected, *rejected]
        result = ReviewResult(confirmed=confirmed, rejected=rejected, verdicts=verdicts)
        state.findings = confirmed
        state.rejected = rejected
        ctx.trace.event("review_summary", stage=self.code, confirmed=len(confirmed),
                        rejected=len(rejected), disputed=len(disputed))
        return result

    def _arbitrate(self, ctx: RunContext, context: str, disputed: list[Dispute],
                   blocks: dict) -> tuple[list[Finding], list[Finding]]:
        if not disputed:
            return [], []
        instructions, ids = prompt_store.compose("s6_arbiter", "severity", "gaps", legend=LEGEND)
        chunks = [disputed[i:i + BATCH_SIZE] for i in range(0, len(disputed), BATCH_SIZE)]
        requests = []
        for n, chunk in enumerate(chunks, 1):
            refs = [f"D{i}" for i in range(1, len(chunk) + 1)]
            items = []
            for ref, (f, v, severity_only) in zip(refs, chunk, strict=True):
                counter = "\n".join(f"  - [{e.block_id}] «{e.quote}»"
                                    for e in (v.counter_evidence if v else []))
                opponent = v.reasoning if v else "Оппонент не дал заключения."
                if v and v.still_missing.strip():
                    opponent += f" Всё ещё не описано: {v.still_missing.strip()}"
                kind = ("\n  dispute: severity only. The opponent confirms the defect but "
                        f"lowers severity {f.severity.value} → {v.severity.value}: "
                        f"{v.severity_reason or v.reasoning}" if severity_only and v else "")
                items.append(_finding_view(ref, f) + kind + f"\n  opponent: {opponent}"
                             + (f"\n  opponent's quotes:\n{counter}" if counter else ""))
            requests.append(dict(
                step=f"s6_arbiter[{n}]", role="arbiter", instructions=instructions,
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
            for i, (f, v, severity_only) in enumerate(chunk, 1):
                d = by_ref.get(f"D{i}")
                objection = v.reasoning if v else "оппонент не дал заключения"
                if severity_only and v is not None:
                    kept.append(_settle_severity(f, v, d))
                    continue
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


class Dispute(NamedTuple):
    """A finding the arbiter decides: refuted by the opponent, or only its severity."""

    finding: Finding
    verdict: Verdict | None
    severity_only: bool = False


def _lowers_critical(f: Finding, v: Verdict) -> bool:
    return f.severity == Severity.CRITICAL and v.severity is not None \
        and v.severity != Severity.CRITICAL


def _settle_severity(f: Finding, v: Verdict, d: Decision | None) -> Finding:
    """The defect stands either way; the arbiter's severity wins, the reviewer's otherwise."""
    severity = (d.severity if d and d.severity else f.severity)
    why = (f"Арбитр: {d.reasoning}" if d else
           "Арбитр не вынес решения, серьёзность оставлена по рецензенту.")
    return f.model_copy(update={
        "status": FindingStatus.CONFIRMED, "severity": severity,
        "criterion": _criterion_after(f, v),
        "trail": [*f.trail, "opponent:severity_disputed", "arbiter:severity"],
        "review_note": f"Оппонент: {v.reasoning} {why}"})
