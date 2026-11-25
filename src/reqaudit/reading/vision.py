"""Vision: what the text layer cannot give, read by a multimodal model.

Two kinds of work:
- a page without a usable text layer is transcribed into blocks;
- a picture on a normal page is described, its text copied and its statements listed.

Results become ordinary blocks with source `vlm` and lower confidence, so later stages can
quote them and the report can say that the text was read from an image. If no vision
model is available, the affected pages and pictures are reported as unread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, field_validator

from reqaudit import prompt_store
from reqaudit.llm.checks import combine, require_russian
from reqaudit.llm.client import LLMClient, LLMError
from reqaudit.models import BlockKind, BlockSource
from reqaudit.reading.assemble import Draft

ROLE = "vision"
VLM_CONFIDENCE = 0.8
PICTURE_KINDS_RU = {
    "diagram": "схема", "chart": "график", "table": "таблица", "screenshot": "макет экрана",
    "photo": "фотография", "logo": "логотип", "decorative": "оформление", "other": "рисунок",
}
_WORD = re.compile(r"[\wё]{3,}", re.IGNORECASE)


class TranscribedBlock(BaseModel):
    kind: Literal["heading", "paragraph", "list_item", "table_row", "caption", "picture"]
    text: str
    level: int | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _unknown_is_paragraph(cls, value: object) -> object:
        known = ("heading", "paragraph", "list_item", "table_row", "caption", "picture")
        return value if value in known else "paragraph"


class PageTranscript(BaseModel):
    blocks: list[TranscribedBlock]


class PictureDescription(BaseModel):
    kind: Literal["diagram", "chart", "table", "screenshot", "photo", "logo", "decorative",
                  "other"]
    description: str
    text_in_image: str = ""
    statements: list[str] = []

    @field_validator("kind", mode="before")
    @classmethod
    def _unknown_is_other(cls, value: object) -> object:
        return value if value in PICTURE_KINDS_RU else "other"


@dataclass
class Picture:
    """A picture waiting for a description: where it is and what surrounds it."""

    png: bytes
    page: int | None
    position: int  # index in the draft list to insert the description before
    context: str


_KIND = {"heading": BlockKind.HEADING, "paragraph": BlockKind.PARAGRAPH,
         "list_item": BlockKind.LIST_ITEM, "table_row": BlockKind.TABLE_ROW,
         "caption": BlockKind.CAPTION, "picture": BlockKind.IMAGE_TEXT}


def picture_text(d: PictureDescription) -> str:
    parts = [f"[Изображение: {PICTURE_KINDS_RU[d.kind]}] {d.description.strip()}"]
    if d.text_in_image.strip():
        parts.append(f"Надписи на изображении: {d.text_in_image.strip()}")
    if d.statements:
        parts.append("Утверждения на изображении: "
                     + "; ".join(s.strip().rstrip(".") for s in d.statements) + ".")
    return "\n".join(parts)


def transcript_drafts(transcript: PageTranscript, page: int) -> list[Draft]:
    drafts: list[Draft] = []
    header: list[str] | None = None
    for block in transcript.blocks:
        kind = _KIND[block.kind]
        text = block.text.strip()
        if kind == BlockKind.TABLE_ROW:
            cells = [c.strip() for c in text.strip("|").split("|")]
            text = "| " + " | ".join(cells) + " |"
            if header is None:
                header, kind = cells, BlockKind.TABLE
        else:
            header = None
        if kind == BlockKind.IMAGE_TEXT:
            text = f"[Изображение] {text}"
        drafts.append(Draft(kind, text, BlockSource.VLM, page=page,
                            heading_level=block.level or (1 if kind == BlockKind.HEADING
                                                          else None),
                            table_header=header if kind in (BlockKind.TABLE,
                                                            BlockKind.TABLE_ROW) else None,
                            confidence=VLM_CONFIDENCE))
    return drafts


def coverage(reference: str, candidate: str) -> float:
    """Share of the reference's words present in the candidate; 1.0 for an empty reference."""
    ref = {w.lower() for w in _WORD.findall(reference)}
    if not ref:
        return 1.0
    have = {w.lower() for w in _WORD.findall(candidate)}
    return len(ref & have) / len(ref)


def run_vision(llm: LLMClient, pages: dict[int, bytes], pictures: list[Picture]
               ) -> tuple[dict[int, PageTranscript | LLMError],
                          list[PictureDescription | LLMError]]:
    """Transcribe pages and describe pictures in parallel."""
    page_prompt, page_ids = prompt_store.compose("vision_page")
    pic_prompt, pic_ids = prompt_store.compose("vision_picture")
    requests = [dict(step=f"vision_page[{n}]", role=ROLE, instructions=page_prompt,
                     prompt=f"Page {n} of the document.", images=[png],
                     output_type=PageTranscript, prompt_ids=page_ids)
                for n, png in pages.items()]
    requests += [dict(step=f"vision_picture[{i + 1}]", role=ROLE, instructions=pic_prompt,
                      prompt=("Text around the picture, for context only:\n<context>\n"
                              f"{p.context[:1500]}\n</context>"),
                      images=[p.png], output_type=PictureDescription, prompt_ids=pic_ids,
                      check=combine(require_russian("description", "statements")))
                 for i, p in enumerate(pictures)]
    results = llm.many(requests) if requests else []
    page_results = dict(zip(pages, results[:len(pages)], strict=True))
    return page_results, results[len(pages):]
