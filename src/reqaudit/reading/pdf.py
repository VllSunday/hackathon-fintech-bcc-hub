"""PDF reader over the text layer (PyMuPDF).

The text layer gives exact characters: Cyrillic, ₸, digits and arrows come through as the
author typed them, which quote checking needs. What the text layer cannot give is left
for vision (see `vision.py`): pages without a usable text layer and embedded pictures
are returned as work items instead of being guessed at.

What this reader does:
- repairs broken files on open (PyMuPDF), opens files with an empty user password;
- reads text blocks with font sizes; headings are found by size relative to body text;
- reads bordered tables into a header row and one block per row;
- marks repeating page headers and footers;
- splits off hidden text (white, below 2 pt, outside the page) into hidden blocks;
- joins words hyphenated across lines; a soft hyphen inside a line is a drawn hyphen;
- grades every page and lists pages whose text layer is missing or garbled.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from reqaudit.errors import InputError
from reqaudit.models import BlockKind, BlockSource
from reqaudit.reading.assemble import Draft, table_row

pymupdf.no_recommend_layout()  # otherwise PyMuPDF advertises an extra package on stdout

_LIST_START = re.compile(r"^\s*(?:[•◦▪●○■\-–*]|\d{1,3}[.)])\s+")
_DIGITS = re.compile(r"\d+")
_BAD_CHARS = re.compile("[�-\x00-\x08\x0b\x0c\x0e-\x1f]")
_LETTER = re.compile(r"[^\W\d_]")
_KNOWN_SCRIPT = re.compile(r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]")
_WHITE = 0xFFFFFF
_EDGE = 0.08  # share of page height where headers and footers live
_MIN_IMAGE_SIDE = 60.0  # points
_MIN_IMAGE_SHARE = 0.03  # of page area


@dataclass
class PageImage:
    page: int
    order: float
    png: bytes


@dataclass
class PageReport:
    page: int
    chars: int
    needs_vision: bool
    reason: str | None = None


@dataclass
class PdfReading:
    drafts: list[Draft]
    pages: list[PageReport]
    images: list[PageImage]
    page_count: int
    repaired: bool
    warnings: list[str] = field(default_factory=list)
    page_pngs: dict[int, bytes] = field(default_factory=dict)


@dataclass
class _Item:
    page: int
    y: float
    x: float
    draft: Draft
    edge_key: str | None = None


def open_pdf(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except Exception as err:  # noqa: BLE001 - any parser failure means the same to the user
        raise InputError("pdf_unreadable", f"PDF не удалось открыть даже с восстановлением: "
                                           f"{err}") from err
    if doc.needs_pass and not doc.authenticate(""):
        raise InputError("pdf_password", "PDF защищён паролем. Снимите пароль или передайте "
                                         "документ без защиты.")
    if doc.page_count == 0:
        raise InputError("pdf_empty", "В PDF нет ни одной страницы.")
    return doc


PAGE_DPI = 150  # resolution of page and picture images sent to vision


def render_pages(path: Path, pages: list[int]) -> dict[int, bytes]:
    """PNG images of the given pages (1-based), for reading them by vision."""
    if not pages:
        return {}
    doc = open_pdf(path)
    return {n: doc[n - 1].get_pixmap(dpi=PAGE_DPI).tobytes("png") for n in pages}


def _clean_line(text: str) -> str:
    # A soft hyphen inside a line is a drawn hyphen; at the line end it marks a word break.
    return text.replace("­", "-").replace(" ", " ")


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in (ln.strip() for ln in lines):
        if not line:
            continue
        if out.endswith("-") and line[:1].islower() and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + line
        else:
            out = f"{out} {line}" if out else line
    return out


def _grade(text: str) -> str | None:
    """Why the page's text layer cannot be trusted, or None."""
    if not text.strip():
        return None
    if len(_BAD_CHARS.findall(text)) > max(3, len(text) * 0.05):
        return "в тексте много нераспознанных символов"
    letters = _LETTER.findall(text)
    if letters and len(_KNOWN_SCRIPT.findall(text)) / len(letters) < 0.6:
        return "текстовый слой похож на испорченную кодировку"
    return None


def _table_rows(page: pymupdf.Page, table) -> list[list[str]]:
    """Cell texts read inside each cell's rectangle in content order.

    `Table.extract()` sorts characters by position and turns "limit_kzt" into "limit kzt _";
    reading the clip in content order keeps the text as written.
    """
    rows = []
    for row in table.rows:
        texts = []
        for cell in row.cells:
            if cell is None:
                texts.append("")
                continue
            raw = page.get_text("text", clip=pymupdf.Rect(cell), sort=False)
            texts.append(_clean_line(" ".join(raw.split())))
        rows.append(texts)
    return rows


def _hidden_reason(span: dict, page_rect: pymupdf.Rect) -> str | None:
    if span["color"] == _WHITE:
        return "белый текст"
    if span["size"] < 2:
        return f"текст размером {span['size']:.1f} pt"
    if not pymupdf.Rect(span["bbox"]).intersects(page_rect):
        return "текст за пределами страницы"
    return None


def _block_kind(text: str, seg: dict, level_of: dict[float, int],
              heading_levels: int) -> tuple[BlockKind, int | None]:
    """Block kind from font size and style of one text segment."""
    sizes, flags = seg["sizes"], seg["flags"]
    size = max(sizes) if sizes else 0.0
    bold = bool(flags) and all(f & 16 for f in flags)
    mono = bool(flags) and all(f & 8 for f in flags)
    if size in level_of and len(text) <= 150 and len(seg["lines"]) <= 3:
        return BlockKind.HEADING, level_of[size]
    if bold and len(text) <= 100 and not text.endswith((".", ":", ";", ",")):
        return BlockKind.HEADING, heading_levels + 1
    if mono:
        return BlockKind.CODE, None
    if _LIST_START.match(text):
        return BlockKind.LIST_ITEM, None
    return BlockKind.PARAGRAPH, None


def read_pdf(path: Path) -> PdfReading:
    doc = open_pdf(path)
    body_sizes: Counter[float] = Counter()
    items: list[_Item] = []
    pages: list[PageReport] = []
    images: list[PageImage] = []
    page_pngs: dict[int, bytes] = {}
    warnings = []
    if doc.is_repaired:
        warnings.append("PDF был повреждён и восстановлен при открытии; проверьте полноту "
                        "текста.")

    raw: list[tuple[int, dict, list[pymupdf.Rect], pymupdf.Rect]] = []
    for page in doc:
        number = page.number + 1
        try:
            tables = page.find_tables().tables
        except Exception:  # noqa: BLE001 - table detection is a bonus, not a requirement
            tables = []
        rects = [pymupdf.Rect(t.bbox) for t in tables]
        # Content-stream order inside lines: sort=True reorders spans by position and turns
        # "limit_kzt" into "limit kzt _" when the underscore sits lower than the letters.
        data = page.get_text("dict")
        raw.append((number, data, rects, page.rect))
        for block in data["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip() and not _hidden_reason(span, page.rect):
                        body_sizes[round(span["size"], 1)] += len(span["text"])

        for t, rect in zip(tables, rects, strict=True):
            rows = _table_rows(page, t)
            if not rows:
                continue
            header = [c.replace("\n", " ").strip() for c in rows[0]]
            items.append(_Item(number, rect.y0, rect.x0, Draft(
                BlockKind.TABLE, table_row(header), BlockSource.TEXT_LAYER, page=number,
                table_header=header)))
            for n, row in enumerate(rows[1:], 1):
                items.append(_Item(number, rect.y0 + n * 0.01, rect.x0, Draft(
                    BlockKind.TABLE_ROW, table_row(row), BlockSource.TEXT_LAYER, page=number,
                    table_header=header)))

        area = page.rect.width * page.rect.height
        for info in page.get_images(full=True):
            for rect in page.get_image_rects(info[0]):
                rect &= page.rect
                if (min(rect.width, rect.height) >= _MIN_IMAGE_SIDE
                        and rect.width * rect.height >= _MIN_IMAGE_SHARE * area):
                    png = page.get_pixmap(clip=rect, dpi=PAGE_DPI).tobytes("png")
                    images.append(PageImage(page=number, order=rect.y0, png=png))

    body = body_sizes.most_common(1)[0][0] if body_sizes else 10.0
    heading_sizes = sorted({s for s in body_sizes if s >= body * 1.08}, reverse=True)
    level_of = {s: n for n, s in enumerate(heading_sizes, 1)}

    for number, data, rects, page_rect in raw:
        visible_chars = 0
        page_text = []
        for block in data["blocks"]:
            if block.get("type") != 0:
                continue
            bbox = pymupdf.Rect(block["bbox"])
            if any(bbox.intersects(r) and (bbox & r).get_area() > 0.5 * bbox.get_area()
                   for r in rects):
                continue
            hidden, reasons = [], set()
            # Segments: a block is split where a line starts a new list item, because PDF
            # producers often put a whole list, i.e. several requirements, into one block.
            segments: list[dict] = []
            for line in block["lines"]:
                shown, sizes, flags = "", [], []
                for span in line["spans"]:
                    reason = _hidden_reason(span, page_rect)
                    if reason:
                        hidden.append(_clean_line(span["text"]))
                        reasons.add(reason)
                    else:
                        shown += span["text"]
                        if span["text"].strip():
                            sizes.append(round(span["size"], 1))
                            flags.append(span["flags"])
                if not shown.strip():
                    continue
                shown = _clean_line(shown)
                if not segments or _LIST_START.match(shown):
                    segments.append({"lines": [], "sizes": [], "flags": [],
                                     "y": line["bbox"][1]})
                segments[-1]["lines"].append(shown)
                segments[-1]["sizes"] += sizes
                segments[-1]["flags"] += flags
            if hidden and "".join(hidden).strip():
                text = _join_lines(hidden)
                items.append(_Item(number, bbox.y0, bbox.x0, Draft(
                    BlockKind.PARAGRAPH, text, BlockSource.TEXT_LAYER, page=number, hidden=True,
                    issues=[("hidden_text", f"На странице {number} скрытый фрагмент "
                                            f"({', '.join(sorted(reasons))}): «{text[:120]}».")])))
            for seg in segments:
                text = _join_lines(seg["lines"])
                visible_chars += len(text)
                page_text.append(text)
                kind, level = _block_kind(text, seg, level_of, len(heading_sizes))
                edge = None
                if (bbox.y1 < page_rect.height * _EDGE
                        or bbox.y0 > page_rect.height * (1 - _EDGE)):
                    edge = _DIGITS.sub("#", text.lower())
                items.append(_Item(number, seg["y"], bbox.x0, Draft(
                    kind, text, BlockSource.TEXT_LAYER, page=number, heading_level=level),
                    edge_key=edge))

        has_image = any(img.page == number for img in images)
        if visible_chars < 40 and has_image:
            pages.append(PageReport(number, visible_chars, True,
                                    "на странице нет текстового слоя, похоже на скан"))
        elif visible_chars == 0:
            pages.append(PageReport(number, 0, False, "пустая страница"))
        else:
            reason = _grade(" ".join(page_text))
            pages.append(PageReport(number, visible_chars, reason is not None, reason))

    # Headers and footers: edge lines that repeat on at least half of the pages.
    repeats = Counter(i.edge_key for i in items if i.edge_key)
    threshold = max(2, doc.page_count // 2)
    for item in items:
        if item.edge_key and repeats[item.edge_key] >= threshold:
            item.draft.kind = BlockKind.HEADER_FOOTER
            item.draft.heading_level = None

    for report in pages:
        if report.needs_vision:
            page_pngs[report.page] = doc[report.page - 1].get_pixmap(dpi=PAGE_DPI).tobytes("png")

    items.sort(key=lambda i: (i.page, i.y, i.x))
    for item in items:
        # Vertical position on the page, used to put picture descriptions in reading order.
        item.draft.bbox = (item.x, item.y, item.x, item.y)
    return PdfReading(drafts=[i.draft for i in items], pages=pages, images=images,
                      page_count=doc.page_count, repaired=doc.is_repaired, warnings=warnings,
                      page_pngs=page_pngs)
