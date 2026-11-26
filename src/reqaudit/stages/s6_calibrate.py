"""Severity calibration, the last step of s6: all confirmed findings rated together.

Reviewers set severities one cell at a time, without seeing each other's findings, and the
opponent and the arbiter judge them in batches. Rated in isolation, each defect tends to look
important. Here one call sees every confirmed finding, names the document's core flow once,
and answers two factual questions per finding (which path it affects; can money, data or a
mandatory check be lost) before giving the level. Code keeps the level consistent with those
answers; within the bounds the scale is the model's to apply.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

from reqaudit import prompt_store
from reqaudit.criteria import Criterion
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.models import Finding, Severity
from reqaudit.reading.render import LEGEND
from reqaudit.stages.base import RunContext

CHUNK = 60  # findings per call; one call keeps them comparable, a huge list is split
# A remark is a gap whose missing piece is below the requirement level (prompts/gaps.md).
# A violation of any other criterion is a quality defect under the standards.
REMARK_CRITERIA = frozenset({Criterion.COMPLETE, Criterion.SET_COMPLETE})


class Rating(BaseModel):
    ref: str
    path: Literal["core", "secondary", "none"]
    harm: bool
    severity: Severity
    reason: str
    remark: bool = False

    @field_validator("path", mode="before")
    @classmethod
    def _unknown_path_is_secondary(cls, value: object) -> object:
        return value if value in ("core", "secondary", "none") else "secondary"


class CalibrationAnswer(BaseModel):
    core_flow: str
    ratings: list[Rating]


def bounds(path: str, harm: bool) -> set[Severity]:
    """Levels the scale allows for these answers."""
    if path == "none":
        return {Severity.MINOR}
    if harm or path == "core":
        return {Severity.CRITICAL, Severity.MAJOR}
    return {Severity.MAJOR}


def _view(ref: str, f: Finding) -> str:
    quotes = "; ".join(f"[{e.block_id}] «{e.quote[:160]}»" for e in f.evidence[:3])
    return (f"{ref}: criterion={f.criterion.value} severity={f.severity.value}\n"
            f"  title: {f.title}\n  explanation: {f.explanation}\n  evidence: {quotes}")


def _check(refs: list[str], findings: dict[str, Finding] | None = None) -> Check:
    findings = findings or {}

    def check(answer: CalibrationAnswer) -> str | None:
        rated = {r.ref for r in answer.ratings}
        complaints = ([f"rate every finding; missing {[r for r in refs if r not in rated]}"]
                      if set(refs) - rated else [])
        for r in answer.ratings:
            allowed = bounds(r.path, r.harm)
            if r.severity not in allowed:
                complaints.append(
                    f"{r.ref}: path={r.path}, harm={str(r.harm).lower()} allows "
                    f"{' or '.join(sorted(s.value for s in allowed))}, not {r.severity.value}; "
                    "change the level or the answer that does not hold")
            if r.remark and r.path != "none":
                complaints.append(f"{r.ref}: a remark changes no behaviour, so its path is "
                                  "`none`; set remark false or path none")
            f = findings.get(r.ref)
            if r.remark and f is not None and f.criterion not in REMARK_CRITERIA:
                complaints.append(f"{r.ref}: only a gap (complete, set_complete) can be a "
                                  f"remark; a {f.criterion.value} finding is a quality defect "
                                  "under the standards: set remark false")
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in complaints)) if complaints else None

    return combine(check, require_russian("core_flow", "ratings.reason"))


def calibrate(ctx: RunContext, context: str, findings: list[Finding]) -> list[Finding]:
    """Findings with calibrated severities; unchanged when the call fails."""
    if not findings:
        return findings
    instructions, ids = prompt_store.compose("s6_calibrate", "severity", legend=LEGEND)
    chunks = [findings[i:i + CHUNK] for i in range(0, len(findings), CHUNK)]
    requests = []
    for n, chunk in enumerate(chunks, 1):
        refs = [f"C{i}" for i in range(1, len(chunk) + 1)]
        listing = "\n\n".join(_view(r, f) for r, f in zip(refs, chunk, strict=True))
        requests.append(dict(step=f"s6_calibrate[{n}]", role="arbiter",
                             instructions=instructions,
                             prompt=f"{context}\n\n<findings>\n{listing}\n</findings>\n",
                             output_type=CalibrationAnswer, prompt_ids=ids,
                             check=_check(refs, dict(zip(refs, chunk, strict=True))),
                             soft_check=True))
    out: list[Finding] = []
    changed = 0
    for chunk, result in zip(chunks, ctx.llm.many(requests), strict=True):
        if isinstance(result, LLMError):
            ctx.trace.event("calibration_failed", error=result.message[:300])
            out.extend(chunk)
            continue
        by_ref = {r.ref: r for r in result.ratings}
        ctx.trace.event("calibration", core_flow=result.core_flow, ratings=[
            {"finding": f.id, "title": f.title, "was": f.severity.value,
             **(by_ref[f"C{i}"].model_dump(mode="json", exclude={"ref"})
                if f"C{i}" in by_ref else {})}
            for i, f in enumerate(chunk, 1)])
        for i, f in enumerate(chunk, 1):
            r = by_ref.get(f"C{i}")
            if r is None or r.severity not in bounds(r.path, r.harm):
                out.append(f)
                continue
            if r.remark and r.path == "none" and f.criterion in REMARK_CRITERIA:
                f = f.model_copy(update={"remark": True,
                                         "trail": [*f.trail, "calibrated:remark"]})
            if r.severity != f.severity:
                changed += 1
                f = f.model_copy(update={
                    "severity": r.severity,
                    "trail": [*f.trail, f"calibrated:{f.severity.value}→{r.severity.value}"],
                    "severity_reason": r.reason})
            out.append(f)
    ctx.trace.event("calibration_summary", findings=len(findings), changed=changed)
    return out
