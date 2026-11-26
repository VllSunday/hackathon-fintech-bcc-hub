"""Pieces shared by the checking stages (s5, s6): prompt context, drafts, quote checks."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

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
    Severity,
    Unit,
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
        allowed = {"analyst", "product_owner", "architect", "developer", "tester", "security"}
        return value if value in allowed else None


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
    conflicts = "\n".join(f"- {c.kind}: {c.summary} (facts {', '.join(c.fact_ids)})"
                          for c in table.conflicts) or "- none"
    return ("<fact_table>\n" + "\n".join(lines) + "\n</fact_table>\n\n"
            f"<code_found_discrepancies>\n{conflicts}\n</code_found_discrepancies>")
