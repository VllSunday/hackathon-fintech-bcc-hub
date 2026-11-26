"""Vision: what the text layer cannot give, read by a multimodal model.

Two kinds of work:
- a page without a usable text layer is transcribed into blocks;
- a picture on a normal page is described, its text copied and its statements listed.

Results become ordinary blocks with source `vlm` and lower confidence, so later stages can
quote them and the report can say that the text was read from an image. If no vision
model is available, the affected pages and pictures are reported as unread.

A scanned page is read twice, by two different strategies: blocks with structure, and
plain lines copied character by character. Code compares the two readings word by word;
where they differ (most often a digit), the blocks get lower confidence and the report
shows both readings, so a defect never rests silently on a misread number.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, field_validator

from reqaudit import prompt_store
from reqaudit.llm.checks import combine, require_russian
from reqaudit.llm.client import LLMClient, LLMError
from reqaudit.models import BlockKind, BlockSource
from reqaudit.reading.assemble import Draft

ROLE = "vision"
VLM_CONFIDENCE = 0.8
DOUBT_CONFIDENCE = 0.6  # a block where the two readings of a scan differ
MAX_DISCREPANCIES = 20  # per page; more means the readings diverge as a whole
# A number with thousand separators («1 000 000») is one token, so a misread digit group
# shows as «1 000 000» / «100 000», not as scattered pieces.
_TOKEN = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|[\wё]+(?:[.,:/][\wё]+)*",
                    re.IGNORECASE)
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


class PageLines(BaseModel):
    """The second, independent reading of a scanned page."""

    lines: list[str]


@dataclass(frozen=True)
class Discrepancy:
    first: str  # words of the first reading, empty when only the second has them
    second: str

    def __str__(self) -> str:
        return f"«{self.first or '—'}» / «{self.second or '—'}»"


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def discrepancies(first: str, second: str) -> list[Discrepancy]:
    """Where two readings of the same page differ, in the first reading's order.

    Words are compared as multisets first, so text read in a different order (a table
    read by rows in one reading and by columns in the other) is not a difference; only
    words one reading has more often than the other are reported, with their neighbours
    in the aligned stretch for context.
    """
    a, b = _tokens(first), _tokens(second)
    extra_a, extra_b = Counter(a) - Counter(b), Counter(b) - Counter(a)
    found: list[Discrepancy] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        mine = [t for t in a[i1:i2] if extra_a[t] > 0]
        theirs = [t for t in b[j1:j2] if extra_b[t] > 0]
        if not mine and not theirs:
            continue  # the same words, only in another place
        for t in mine:
            extra_a[t] -= 1
        for t in theirs:
            extra_b[t] -= 1
        found.append(Discrepancy(" ".join(a[i1:i2][:12]), " ".join(b[j1:j2][:12])))
    return found


def mark_doubts(drafts: list[Draft], found: list[Discrepancy]) -> list[Discrepancy]:
    """Lower the confidence of blocks holding a disputed reading; return the unplaced ones."""
    unplaced = []
    for d in found:
        words = _tokens(d.first)
        target = next((draft for draft in drafts
                       if words and _contains(_tokens(draft.text), words)), None)
        if target is None:
            unplaced.append(d)
            continue
        target.confidence = min(target.confidence, DOUBT_CONFIDENCE)
        target.issues.append(("scan_discrepancy",
                              f"Два прочтения скана расходятся: {d} (первое / второе)."))
    return unplaced


def _contains(tokens: list[str], words: list[str]) -> bool:
    n = len(words)
    return any(tokens[i:i + n] == words for i in range(len(tokens) - n + 1))


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


def lines_drafts(reading: PageLines, page: int) -> list[Draft]:
    """Blocks from the line-by-line reading, used when the structured one failed."""
    return [Draft(BlockKind.TABLE_ROW if line.count("|") >= 2 else BlockKind.PARAGRAPH,
                  line.strip(), BlockSource.VLM, page=page, confidence=DOUBT_CONFIDENCE)
            for line in reading.lines if line.strip()]


def coverage(reference: str, candidate: str) -> float:
    """Share of the reference's words present in the candidate; 1.0 for an empty reference."""
    ref = {w.lower() for w in _WORD.findall(reference)}
    if not ref:
        return 1.0
    have = {w.lower() for w in _WORD.findall(candidate)}
    return len(ref & have) / len(ref)


def run_vision(llm: LLMClient, pages: dict[int, bytes], pictures: list[Picture],
               second: dict[int, bytes] | None = None
               ) -> tuple[dict[int, PageTranscript | LLMError],
                          list[PictureDescription | LLMError],
                          dict[int, PageLines | LLMError]]:
    """Transcribe pages, describe pictures and read the `second` pages again, in parallel."""
    second = second or {}
    page_prompt, page_ids = prompt_store.compose("vision_page")
    pic_prompt, pic_ids = prompt_store.compose("vision_picture")
    check_prompt, check_ids = prompt_store.compose("vision_page_check")
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
    requests += [dict(step=f"vision_page_check[{n}]", role=ROLE, instructions=check_prompt,
                      prompt=f"Page {n} of the document.", images=[png],
                      output_type=PageLines, prompt_ids=check_ids)
                 for n, png in second.items()]
    results = llm.many(requests) if requests else []
    page_results = dict(zip(pages, results[:len(pages)], strict=True))
    tail = len(pages) + len(pictures)
    second_results = dict(zip(second, results[tail:], strict=True))
    return page_results, results[len(pages):tail], second_results
