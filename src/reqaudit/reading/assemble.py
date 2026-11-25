"""Turn readers' draft blocks into final blocks: ids, section paths, page anchors.

Every reader (PDF, DOCX, Docling, vision) produces `Draft` blocks in reading order. This
module gives them ids and section paths in one way, so the rest of the pipeline does
not care which reader saw the document.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reqaudit.models import Anchor, Block, BlockKind, BlockSource, ReadIssue


@dataclass
class Draft:
    kind: BlockKind
    text: str
    source: BlockSource
    page: int | None = None
    paragraph: int | None = None
    heading_level: int | None = None
    table_header: list[str] | None = None
    hidden: bool = False
    confidence: float = 1.0
    bbox: tuple[float, float, float, float] | None = None
    issues: list[tuple[str, str]] = field(default_factory=list)  # (kind, message)


def table_row(cells: list[str]) -> str:
    return "| " + " | ".join(c.replace("\n", " ").strip() for c in cells) + " |"


def finalize(drafts: list[Draft]) -> tuple[list[Block], list[ReadIssue]]:
    blocks: list[Block] = []
    issues: list[ReadIssue] = []
    sections: list[tuple[int, str]] = []
    for draft in drafts:
        text = draft.text.strip()
        if not text:
            continue
        if draft.kind == BlockKind.HEADING and not draft.hidden:
            level = draft.heading_level or 1
            while sections and sections[-1][0] >= level:
                sections.pop()
            sections.append((level, text.lstrip("#").strip()))
        block = Block(
            id=f"b{len(blocks) + 1:04d}",
            kind=draft.kind,
            text=text,
            anchor=Anchor(page=draft.page, paragraph=draft.paragraph,
                          section_path=[t for _, t in sections]),
            source=draft.source,
            confidence=draft.confidence,
            hidden=draft.hidden,
            table_header=draft.table_header,
        )
        blocks.append(block)
        for kind, message in draft.issues:
            issues.append(ReadIssue(kind=kind, message=message, block_id=block.id))
    return blocks, issues
