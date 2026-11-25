"""s2 Profile and segmentation.

The model decides what counts as a requirement; code decides what is admissible:

- every unit points to existing blocks and quotes text that is really in them;
- a document label such as 'Ф-1' really occurs in the unit's blocks;
- every visible block other than headings and table headers is either in a unit
  or explicitly skipped with a reason.

Violations go back to the model as a concrete complaint. What is still wrong after the
last retry is repaired or dropped by code and recorded in the coverage report, so a
partially bad answer never sinks the whole run and never reaches the report unchecked.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from reqaudit import prompt_store
from reqaudit.evidence import (
    MIN_KEY_LENGTH,
    blocks_key,
    has_ellipsis,
    locate,
    match_key,
    quote_in,
)
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.models import (
    Block,
    BlockKind,
    Coverage,
    DocumentIR,
    DocumentProfile,
    Segmentation,
    SkippedBlock,
    SkipReason,
    Unit,
    UnitKind,
)
from reqaudit.reading.render import LEGEND, render_blocks, render_outline, split_windows, visible
from reqaudit.stages.base import RunContext, Stage

ROLE = "analyst"
_STRUCTURAL = {BlockKind.HEADING, BlockKind.TABLE}
_MAX_COMPLAINTS = 25


class UnitDraft(BaseModel):
    block_ids: list[str] = Field(min_length=1)
    doc_label: str | None = None
    # A skip reason here means the model judged the block not to be a requirement but put it
    # in the wrong list; code moves it to `skipped` instead of rejecting the whole answer.
    kind: UnitKind | SkipReason
    quote: str
    summary: str


class SegmentAnswer(BaseModel):
    notes: str
    units: list[UnitDraft]
    skipped: list[SkippedBlock]


# --- code checks ------------------------------------------------------------------------


def _clean_label(label: str | None) -> str | None:
    if label is None:
        return None
    label = label.strip().strip(".:;,").strip()
    return label or None


def unit_problems(draft: UnitDraft, by_id: dict[str, Block]) -> list[str]:
    problems = []
    unknown = [i for i in draft.block_ids if i not in by_id]
    if unknown:
        problems.append(f"block ids {unknown} are not in this part of the document")
    blocks = [by_id[i] for i in draft.block_ids if i in by_id]
    if not blocks:
        return problems
    if has_ellipsis(draft.quote):
        problems.append("the quote uses an ellipsis; quote the statement whole")
    elif len(match_key(draft.quote)) < MIN_KEY_LENGTH:
        problems.append("the quote is too short to locate reliably; quote the whole sentence "
                        "or the whole table row including its field name, e.g. "
                        "'REST type | GET' rather than 'GET'")
    elif not quote_in(draft.quote, blocks):
        found = locate(draft.quote, list(by_id.values()))
        hint = f"; this text occurs in {found}" if found else "; this text is not in the document"
        problems.append(f"the quote is not found in blocks {draft.block_ids}{hint}")
    label = _clean_label(draft.doc_label)
    if label and match_key(label) not in blocks_key(blocks):
        problems.append(f"doc_label '{label}' does not occur in its blocks")
    return problems


def _needed(window: list[Block]) -> list[str]:
    return [b.id for b in visible(window) if b.kind not in _STRUCTURAL]


def segment_check(window: list[Block]) -> Check:
    by_id = {b.id: b for b in visible(window)}
    needed = _needed(window)

    def check(answer: SegmentAnswer) -> str | None:
        complaints = []
        for n, draft in enumerate(answer.units):
            for problem in unit_problems(draft, by_id):
                complaints.append(f"units[{n}] (quote starts {draft.quote[:40]!r}): {problem}")
        bad_skips = [s.block_id for s in answer.skipped if s.block_id not in by_id]
        if bad_skips:
            complaints.append(f"skipped lists block ids not in this part: {bad_skips}")
        covered = {i for d in answer.units for i in d.block_ids} | {s.block_id
                                                                     for s in answer.skipped}
        missing = [i for i in needed if i not in covered]
        if missing:
            complaints.append(f"blocks {missing} are neither in a unit nor in skipped; "
                              "add each to a unit or to skipped with a reason")
        if not complaints:
            return None
        listed = complaints[:_MAX_COMPLAINTS]
        more = len(complaints) - len(listed)
        text = "Fix these problems and return the full answer again:\n" + "\n".join(
            f"- {c}" for c in listed)
        return text + (f"\n- and {more} more of the same kind" if more > 0 else "")

    return combine(check, require_russian("notes", "units.summary"))


def profile_check(document: DocumentIR) -> Check:
    order = {b.id: n for n, b in enumerate(document.blocks)}

    def check(profile: DocumentProfile) -> str | None:
        problems = []
        names = {p.name for p in profile.parts}
        for part in profile.parts:
            if part.start_block_id not in order:
                problems.append(f"part '{part.name}': block {part.start_block_id} does not exist")
            if part.parent and part.parent not in names:
                problems.append(f"part '{part.name}': parent '{part.parent}' is not a listed part")
        starts = [order[p.start_block_id] for p in profile.parts if p.start_block_id in order]
        if starts != sorted(starts):
            problems.append("parts must be listed in document order")
        return "; ".join(problems) or None

    return combine(check, require_russian("summary"))


# --- prompts ----------------------------------------------------------------------------


def _profile_prompt(document: DocumentIR, window_chars: int) -> str:
    full = render_blocks(document.blocks)
    if len(full) <= window_chars:
        return f"The whole document:\n<document>\n{full}\n</document>\n"
    return ("The document is too long to show whole. Its outline (headings and table header "
            f"rows) follows.\n<document_outline>\n{render_outline(document.blocks)}\n"
            "</document_outline>\n")


def _segment_prompt(profile: DocumentProfile, window: list[Block], k: int, n: int) -> str:
    shown = visible(window)
    scope = ("This is the whole document." if n == 1 else
             f"This is window {k} of {n}: blocks {shown[0].id} to {shown[-1].id}. "
             "Account for the blocks of this window only.")
    return (f"Profile of the whole document:\n<profile>\n{profile.model_dump_json(indent=2)}\n"
            f"</profile>\n\n{scope}\n<document>\n{render_blocks(window)}\n</document>\n")


# --- assembly ---------------------------------------------------------------------------


def _repair(draft: UnitDraft, by_id: dict[str, Block]) -> tuple[UnitDraft | None, str | None]:
    """Make a draft admissible or explain in Russian why it was dropped.

    Repairs are mechanical only: unknown block ids are removed, a quote found in another
    single block moves the unit there, a label absent from the text is cleared.
    """
    short = draft.quote[:80]
    if has_ellipsis(draft.quote):
        return None, f"Цитата «{short}» сокращена многоточием, её нельзя сверить с документом"
    known = [i for i in draft.block_ids if i in by_id]
    blocks = [by_id[i] for i in known]
    if not blocks or not quote_in(draft.quote, blocks):
        found = locate(draft.quote, list(by_id.values()))
        if not found:
            return None, f"Цитата «{short}» не найдена в документе"
        known, blocks = found[:1], [by_id[found[0]]]
    label = _clean_label(draft.doc_label)
    if label and match_key(label) not in blocks_key(blocks):
        label = None
    return draft.model_copy(update={"block_ids": known, "doc_label": label}), None


def _part_of(index: int, starts: list[tuple[int, str]]) -> str | None:
    part = None
    for start, name in starts:
        if start <= index:
            part = name
    return part


def assemble(document: DocumentIR, profile: DocumentProfile,
             answers: list[tuple[list[Block], SegmentAnswer]]) -> Segmentation:
    order = {b.id: n for n, b in enumerate(document.blocks)}
    starts = sorted((order[p.start_block_id], p.name) for p in profile.parts
                    if p.start_block_id in order)

    drafts: list[UnitDraft] = []
    skipped: dict[str, SkippedBlock] = {}
    dropped: list[str] = []
    needed: list[str] = []
    for window, answer in answers:
        by_id = {b.id: b for b in visible(window)}
        needed.extend(_needed(window))
        for draft in answer.units:
            if isinstance(draft.kind, SkipReason):
                for block_id in draft.block_ids:
                    if block_id in by_id:
                        skipped.setdefault(block_id, SkippedBlock(block_id=block_id,
                                                                  reason=draft.kind))
                continue
            fixed, reason = _repair(draft, by_id)
            if fixed is None:
                dropped.append(reason)
            else:
                drafts.append(fixed)
        for skip in answer.skipped:
            if skip.block_id in by_id:
                skipped.setdefault(skip.block_id, skip)

    by_id_all = {b.id: b for b in document.blocks}
    seen: set[tuple[tuple[str, ...], str]] = set()
    unique: list[UnitDraft] = []
    for draft in drafts:
        key = (tuple(draft.block_ids), match_key(draft.quote))
        if key not in seen:
            seen.add(key)
            unique.append(draft)

    def position(draft: UnitDraft) -> tuple[int, int]:
        first = min(order[i] for i in draft.block_ids)
        text = blocks_key(by_id_all[i] for i in draft.block_ids)
        return first, text.find(match_key(draft.quote))

    unique.sort(key=position)
    units = [
        Unit(id=f"R-{n:03d}", doc_label=_clean_label(d.doc_label), kind=d.kind,
             part=_part_of(position(d)[0], starts), block_ids=d.block_ids,
             quote=d.quote.strip(), summary=d.summary.strip())
        for n, d in enumerate(unique, 1)
    ]

    in_units = {i for u in units for i in u.block_ids}
    unclassified = [i for i in needed if i not in in_units and i not in skipped]
    by_reason: dict[str, int] = {}
    for skip in skipped.values():
        if skip.block_id not in in_units:
            by_reason[skip.reason.value] = by_reason.get(skip.reason.value, 0) + 1

    coverage = Coverage(
        blocks_checked=len(needed),
        in_units=sum(1 for i in needed if i in in_units),
        skipped=sum(1 for i in needed if i not in in_units and i in skipped),
        unclassified=unclassified,
        skipped_by_reason=by_reason,
        dropped_units=dropped,
    )
    return Segmentation(profile=profile, units=units, skipped=list(skipped.values()),
                        coverage=coverage)


class SegmentStage(Stage):
    code = "s2"
    title = "Профиль и нарезка"
    artifact = "s2_units.json"

    def run(self, ctx: RunContext) -> Segmentation:
        document = ctx.state.document
        assert document is not None, "s2 needs s1"
        window_chars = ctx.settings.window_chars

        instructions, ids = prompt_store.compose("s2_profile", legend=LEGEND)
        profile = ctx.llm.structured(
            step="s2_profile", role=ROLE, instructions=instructions,
            prompt=_profile_prompt(document, window_chars), output_type=DocumentProfile,
            prompt_ids=ids, check=profile_check(document))

        instructions, ids = prompt_store.compose("s2_segment", legend=LEGEND)
        windows = split_windows(document.blocks, window_chars)
        answers = []
        for k, window in enumerate(windows, 1):
            answer = ctx.llm.structured(
                step=f"s2_segment[{k}/{len(windows)}]", role=ROLE, instructions=instructions,
                prompt=_segment_prompt(profile, window, k, len(windows)),
                output_type=SegmentAnswer, prompt_ids=ids, check=segment_check(window))
            answers.append((window, answer))

        result = assemble(document, profile, answers)
        ctx.state.profile = result.profile
        ctx.state.units = result.units
        ctx.state.coverage = result.coverage
        ctx.trace.event("segmentation_summary", stage=self.code, units=len(result.units),
                        windows=len(windows), unclassified=len(result.coverage.unclassified),
                        dropped=len(result.coverage.dropped_units))
        return result
