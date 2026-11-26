"""Severity calibration, the last step of s6: all confirmed findings rated together.

Rated one at a time, every defect looks important, and a model shown a reviewer's level tends
to confirm it. So the calibrator works blind and does not choose a level at all. It answers
factual questions about each finding: which path it affects, what goes wrong if the team
guesses, what kind of defect it is, whether it lies on the successful path or a branch of
refusal or failure, and whether money, data
or a mandatory check can be lost. Code turns the answers into the level by one table
(`level`). Several independent ratings, each with the findings in another order, are taken
and the median level wins: one sample of a model's judgement is not a measurement.
"""

from __future__ import annotations

import random
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
# A remark is a gap whose missing piece changes nothing observable (prompts/gaps.md).
# A violation of any other criterion is a quality defect under the standards.
REMARK_CRITERIA = frozenset({Criterion.COMPLETE, Criterion.SET_COMPLETE})

_PATHS = ("core", "secondary")
_CONSEQUENCES = ("outcome", "detail", "none")
_KINDS = ("conflict", "ambiguity", "gap", "other")
_BRANCHES = ("success", "failure")


class Rating(BaseModel):
    ref: str
    path: Literal["core", "secondary"]
    consequence: Literal["outcome", "detail", "none"]
    kind: Literal["conflict", "ambiguity", "gap", "other"]
    branch: Literal["success", "failure"]
    harm: bool
    reason: str

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: object) -> object:
        return value if value in _PATHS else "secondary"

    @field_validator("consequence", mode="before")
    @classmethod
    def _consequence(cls, value: object) -> object:
        return value if value in _CONSEQUENCES else "detail"

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, value: object) -> object:
        return value if value in _KINDS else "other"

    @field_validator("branch", mode="before")
    @classmethod
    def _branch(cls, value: object) -> object:
        return value if value in _BRANCHES else "success"


class CalibrationAnswer(BaseModel):
    core_flow: str
    ratings: list[Rating]


def level(r: Rating) -> Severity:
    """The severity scale (prompts/severity.md) as one table over the calibrator's answers."""
    if r.harm:
        return Severity.CRITICAL
    if r.consequence == "none":
        return Severity.MINOR
    if (r.path == "core" and r.consequence == "outcome"
            and (r.kind in ("conflict", "ambiguity") or r.branch == "success")):
        return Severity.CRITICAL
    return Severity.MAJOR


def is_remark(f: Finding, r: Rating) -> bool:
    """A gap that changes nothing observable: listed apart from the defects, never dropped."""
    return f.criterion in REMARK_CRITERIA and r.consequence == "none" and not r.harm


def _view(ref: str, f: Finding) -> str:
    # Blind: no reviewer's severity, so the calibrator cannot simply confirm it.
    quotes = "; ".join(f"[{e.block_id}] «{e.quote[:160]}»" for e in f.evidence[:3])
    return (f"{ref}: criterion={f.criterion.value}\n"
            f"  title: {f.title}\n  explanation: {f.explanation}\n  evidence: {quotes}")


def _check(refs: list[str]) -> Check:
    def check(answer: CalibrationAnswer) -> str | None:
        rated = {r.ref for r in answer.ratings}
        missing = [r for r in refs if r not in rated]
        return (f"Fix these problems and return the full answer again:\n- rate every "
                f"finding; missing {missing}") if missing else None

    return combine(check, require_russian("core_flow", "ratings.reason"))


def median_level(levels: list[Severity]) -> Severity:
    """The median level; with an even count, the less severe of the two middle ones."""
    ranks = sorted(s.rank for s in levels)
    middle = ranks[len(ranks) // 2]
    return next(s for s in Severity if s.rank == middle)


def calibrate(ctx: RunContext, context: str, findings: list[Finding]) -> list[Finding]:
    """Findings with calibrated severities; unchanged when every rating call fails."""
    if not findings:
        return findings
    samples = max(1, ctx.settings.calibration_samples)
    instructions, ids = prompt_store.compose("s6_calibrate", "severity", legend=LEGEND)
    chunks = [findings[i:i + CHUNK] for i in range(0, len(findings), CHUNK)]
    requests, owners = [], []
    for n, chunk in enumerate(chunks, 1):
        for k in range(1, samples + 1):
            # Each sample sees the findings in its own order: position must not decide.
            order = list(range(len(chunk)))
            if k > 1:
                random.Random(k).shuffle(order)
            refs = {i: f"C{p}" for p, i in enumerate(order, 1)}
            listing = "\n\n".join(_view(refs[i], chunk[i]) for i in order)
            requests.append(dict(
                step=f"s6_calibrate[{n}]" if k == 1 else f"s6_calibrate[{n}/{k}]",
                role="arbiter", instructions=instructions,
                prompt=f"{context}\n\n<findings>\n{listing}\n</findings>\n",
                output_type=CalibrationAnswer, prompt_ids=ids,
                check=_check(list(refs.values())), soft_check=True))
            owners.append((n - 1, refs))

    ratings: dict[str, list[Rating]] = {}
    for (c, refs), result in zip(owners, ctx.llm.many(requests), strict=True):
        if isinstance(result, LLMError):
            ctx.trace.event("calibration_failed", error=result.message[:300])
            continue
        by_ref = {r.ref: r for r in result.ratings}
        chunk = chunks[c]
        ctx.trace.event("calibration", core_flow=result.core_flow, ratings=[
            {"finding": chunk[i].id, "title": chunk[i].title, "was": chunk[i].severity.value,
             **({**by_ref[ref].model_dump(mode="json", exclude={"ref"}),
                 "level": level(by_ref[ref]).value} if ref in by_ref else {})}
            for i, ref in refs.items()])
        for i, ref in refs.items():
            if ref in by_ref:
                ratings.setdefault(chunk[i].id, []).append(by_ref[ref])

    out: list[Finding] = []
    changed = 0
    for f in findings:
        mine = ratings.get(f.id)
        if not mine:
            out.append(f)
            continue
        new = median_level([level(r) for r in mine])
        chosen = next(r for r in mine if level(r) == new)
        if sum(is_remark(f, r) for r in mine) * 2 > len(mine):
            f = f.model_copy(update={"remark": True, "trail": [*f.trail, "calibrated:remark"]})
        if new != f.severity:
            changed += 1
            f = f.model_copy(update={
                "severity": new,
                "trail": [*f.trail, f"calibrated:{f.severity.value}→{new.value}"],
                "severity_reason": chosen.reason})
        out.append(f)
    ctx.trace.event("calibration_summary", findings=len(findings), changed=changed,
                    samples=samples)
    return out
