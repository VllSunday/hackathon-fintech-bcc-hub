"""Pieces shared by the checking stages (s5, s6): prompt context, drafts, quote checks."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal, get_args

from pydantic import BaseModel, field_validator

from reqaudit.criteria import Criterion
from reqaudit.evidence import MIN_KEY_LENGTH, has_ellipsis, locate, match_key, quote_in
from reqaudit.models import (
    Block,
    DocumentIR,
    DocumentProfile,
    Evidence,
    Fact,
    FactTable,
    Finding,
    FindingStatus,
    Severity,
    Unit,
    scope_of,
)
from reqaudit.reading.render import render_blocks, render_outline

QuestionTo = Literal["analyst", "product_owner", "architect", "developer", "tester", "security"]


class EvidenceDraft(BaseModel):
    block_id: str
    quote: str


class CandidateDraft(BaseModel):
    """A finding as the reviewer model returns it. Lenient on purpose: code drops what is wrong."""

    criterion: Criterion | str
    unit_ids: list[str] = []
    evidence: list[EvidenceDraft]
    title: str
    explanation: str
    severity: Severity
    suggestion: str
    question: str | None = None
    question_to: QuestionTo | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _unknown_severity_is_major(cls, value: object) -> object:
        return value if value in {s.value for s in Severity} else Severity.MAJOR.value

    @field_validator("question_to", mode="before")
    @classmethod
    def _unknown_addressee_is_none(cls, value: object) -> object:
        return value if value in get_args(QuestionTo) else None


# --- prompt context ---------------------------------------------------------------------


def profile_view(profile: DocumentProfile) -> str:
    parts = "\n".join(f"- {p.name} ({p.role.value}"
                      + (f", details: {p.parent}" if p.parent else "") + ")"
                      for p in profile.parts)
    return (f"<profile>\n{profile.summary}\nParts:\n{parts}\nNumbering: {profile.numbering}\n"
            "</profile>")


def requirement_index(units: list[Unit]) -> str:
    lines = [f"{u.id} [{u.doc_label or '—'}] {u.kind.value} · {u.part or '—'} · "
             f"blocks {', '.join(u.block_ids)}: {u.summary}" for u in units]
    return "<requirement_index>\n" + "\n".join(lines) + "\n</requirement_index>"


def document_view(document: DocumentIR, focus_blocks: set[str], window_chars: int) -> str:
    """The whole document if it fits; otherwise its outline plus the sections in focus."""
    full = render_blocks(document.blocks)
    if len(full) <= window_chars:
        return f"<document>\n{full}\n</document>"
    sections = {tuple(b.anchor.section_path) for b in document.blocks if b.id in focus_blocks}
    shown = [b for b in document.blocks
             if b.id in focus_blocks or tuple(b.anchor.section_path) in sections]
    return ("<document_outline>\n" + render_outline(document.blocks) + "\n</document_outline>\n"
            "<document_excerpt>\n" + render_blocks(shown) + "\n</document_excerpt>")


# --- quote checks -----------------------------------------------------------------------


def evidence_problem(draft: EvidenceDraft, blocks: dict[str, Block]) -> str | None:
    """None when the quote is really in its block; otherwise a complaint for the model."""
    block = blocks.get(draft.block_id)
    if has_ellipsis(draft.quote):
        return f"quote {draft.quote[:40]!r} uses an ellipsis; quote the words whole"
    if len(match_key(draft.quote)) < MIN_KEY_LENGTH:
        return (f"quote {draft.quote!r} is too short to locate; quote the whole phrase or "
                "table row")
    if block is not None and quote_in(draft.quote, [block]):
        return None
    found = locate(draft.quote, list(blocks.values()))
    if found:
        return f"quote {draft.quote[:40]!r} is not in {draft.block_id}; it occurs in {found}"
    return f"quote {draft.quote[:40]!r} is not in the document"


def verify_evidence(draft: EvidenceDraft, blocks: dict[str, Block]) -> Evidence | None:
    """Evidence with a corrected block id, or None if the quote is not in the document."""
    if has_ellipsis(draft.quote) or len(match_key(draft.quote)) < MIN_KEY_LENGTH:
        return None
    block = blocks.get(draft.block_id)
    if block is not None and quote_in(draft.quote, [block]):
        return Evidence(block_id=block.id, quote=draft.quote.strip(), verified=True)
    found = locate(draft.quote, list(blocks.values()))
    if found:
        return Evidence(block_id=found[0], quote=draft.quote.strip(), verified=True)
    return None


def fact_table_view(table: FactTable) -> str:
    """Facts grouped by entity, then the conflict candidates, for the values reviewer."""
    by_entity: dict[str, list[Fact]] = defaultdict(list)
    for f in table.facts:
        by_entity[f.entity].append(f)
    lines = []
    for entity, facts in by_entity.items():
        lines.append(f"{entity} — {facts[0].entity_label}")
        for f in facts:
            cond = f" [условие: {f.condition}]" if f.condition else ""
            lines.append(f"  {f.id} {f.attribute} = {f.value}{cond} · [{f.block_id}] «{f.quote}»")
    conflicts = "\n".join(f"- K{n} {c.kind}: {c.summary} (facts {', '.join(c.fact_ids)})"
                          for n, c in enumerate(table.conflicts, 1)) or "- none"
    return ("<fact_table>\n" + "\n".join(lines) + "\n</fact_table>\n\n"
            f"<code_found_discrepancies>\n{conflicts}\n</code_found_discrepancies>")


# --- evidence gates, shared by s6 (before the opponent) and s7 (final) ---------------------


@dataclass
class Screened:
    passed: list[Finding]
    rejected: list[Finding]
    stats: dict[str, int]


def one_sided(f: Finding) -> bool:
    """A conflict that quotes only one statement."""
    distinct = {(e.block_id, match_key(e.quote)) for e in f.evidence}
    return f.criterion == Criterion.SET_CONSISTENT and len(distinct) < 2


def screen(findings: list[Finding], blocks: dict[str, Block], units: list[Unit],
           drop_one_sided: bool = True) -> Screened:
    """Code's evidence checks: quotes must be in the document, a conflict needs two sides,
    requirement ids must exist (missing ones are taken from the quoted blocks)."""
    unit_ids = {u.id for u in units}
    units_of_block: dict[str, list[str]] = {}
    for u in units:
        for b in u.block_ids:
            units_of_block.setdefault(b, []).append(u.id)
    passed: list[Finding] = []
    rejected: list[Finding] = []
    stats = {"no_evidence": 0, "one_sided_conflict": 0}
    for f in findings:
        evidence = [v for e in f.evidence
                    if (v := verify_evidence(EvidenceDraft(block_id=e.block_id, quote=e.quote),
                                             blocks))]
        if not evidence:
            stats["no_evidence"] += 1
            rejected.append(f.model_copy(update={
                "status": FindingStatus.REJECTED, "trail": [*f.trail, "gate:no_evidence"],
                "review_note": "Ни одна цитата не найдена в документе."}))
            continue
        checked = f.model_copy(update={"evidence": evidence})
        if drop_one_sided and one_sided(checked):
            stats["one_sided_conflict"] += 1
            rejected.append(f.model_copy(update={
                "status": FindingStatus.REJECTED, "trail": [*f.trail, "gate:one_sided"],
                "review_note": "Противоречие подтверждено цитатой только одной стороны; ни "
                               "рецензент, ни оппонент не привели вторую."}))
            continue
        ids = [u for u in f.unit_ids if u in unit_ids]
        if not ids:
            ids = sorted({u for e in evidence for u in units_of_block.get(e.block_id, [])})
        passed.append(checked.model_copy(update={"unit_ids": ids,
                                                 "scope": scope_of(f.criterion, ids)}))
    return Screened(passed, rejected, stats)


_TITLE_WORD = re.compile(r"[\wё]{4,}", re.IGNORECASE)


def _title_overlap(a: Finding, b: Finding) -> float:
    wa = {w.lower()[:6] for w in _TITLE_WORD.findall(a.title)}
    wb = {w.lower()[:6] for w in _TITLE_WORD.findall(b.title)}
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


def _same_defect(a: Finding, b: Finding) -> bool:
    """Only duplicates code can be sure of; the rest is the editor's call (s8).

    The same two or more places quoted by both are one defect, whatever criterion each
    used. One shared place is not enough: a requirement can have several defects quoted
    from the same sentence (a missing default and a missing rounding rule), so one place
    merges only under the same criterion and with titles naming the same thing.
    """
    blocks_a = {e.block_id for e in a.evidence}
    blocks_b = {e.block_id for e in b.evidence}
    if blocks_a != blocks_b:
        return False
    if len(blocks_a) >= 2:
        return True
    return a.criterion == b.criterion and _title_overlap(a, b) >= 0.5


def merge_duplicates(findings: list[Finding]) -> tuple[list[Finding], int]:
    ordered = sorted(findings, key=lambda f: f.severity.rank)
    kept: list[Finding] = []
    merged = 0
    for f in ordered:
        twin = next((k for k in kept if _same_defect(k, f)), None)
        if twin is None:
            kept.append(f)
            continue
        merged += 1
        index = kept.index(twin)
        kept[index] = twin.model_copy(update={
            "unit_ids": sorted(set(twin.unit_ids) | set(f.unit_ids)),
            "trail": [*twin.trail, f"merged:{f.id}"],
            "question": twin.question or f.question,
        })
    return kept, merged
