"""Docling as the structural reader of PDFs: layout, reading order, headings, tables.

Docling is optional (extra `docling`, about 1.2 GB with torch). It is never trusted alone:
its text is checked page by page against the PyMuPDF text layer, a page where Docling
lost words falls back to the text layer, and hidden text, which Docling does not
distinguish, always comes from the text layer. Pictures and scanned pages are read by
vision, so Docling's OCR is switched off and its text comes from the PDF itself.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from reqaudit.models import BlockKind, BlockSource
from reqaudit.reading.assemble import Draft, table_row
from reqaudit.reading.pdf import PdfReading
from reqaudit.reading.vision import coverage

_KEEP = 0.9  # Docling page text must contain this share of the text layer's words


@dataclass
class Structured:
    drafts: list[Draft]
    error: str | None = None


def available() -> bool:
    return importlib.util.find_spec("docling") is not None


def _clean(text: str) -> str:
    return " ".join(text.replace("­", "-").replace(" ", " ").split())


def read(path: Path) -> Structured:
    if not available():
        return Structured([], "Docling не установлен, PDF прочитан по текстовому слою. "
                              "Установка: команда с reqaudit[docling], см. README.")
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import ContentLayer, DocItemLabel

        options = PdfPipelineOptions(do_ocr=False, force_backend_text=True,
                                     do_table_structure=True)
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        doc = converter.convert(path).document
    except Exception as err:  # noqa: BLE001 - Docling failing must not stop the run
        return Structured([], f"Docling не смог прочитать PDF ({type(err).__name__}: {err}); "
                              "использован текстовый слой.")

    drafts: list[Draft] = []
    layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
    for item, _level in doc.iterate_items(included_content_layers=layers):
        label = getattr(item, "label", None)
        prov = item.prov[0] if getattr(item, "prov", None) else None
        page = prov.page_no if prov else None
        top = 0.0
        if prov is not None and page in doc.pages:
            height = doc.pages[page].size.height
            box = prov.bbox.to_top_left_origin(page_height=height)
            top = box.t
        bbox = (0.0, top, 0.0, top)

        if label == DocItemLabel.TABLE:
            grid = item.data.grid
            if not grid:
                continue
            header = [_clean(c.text) for c in grid[0]]
            drafts.append(Draft(BlockKind.TABLE, table_row(header), BlockSource.STRUCTURE,
                                page=page, table_header=header, bbox=bbox))
            for n, row in enumerate(grid[1:], 1):
                drafts.append(Draft(BlockKind.TABLE_ROW, table_row([_clean(c.text) for c in row]),
                                    BlockSource.STRUCTURE, page=page, table_header=header,
                                    bbox=(0.0, top + n * 0.01, 0.0, top + n * 0.01)))
            continue
        if label == DocItemLabel.PICTURE:
            continue  # pictures are read by vision from the PyMuPDF images
        text = _clean(getattr(item, "text", "") or "")
        if not text:
            continue
        kind, level = BlockKind.PARAGRAPH, None
        if label == DocItemLabel.TITLE:
            kind, level = BlockKind.HEADING, 1
        elif label == DocItemLabel.SECTION_HEADER:
            kind, level = BlockKind.HEADING, (getattr(item, "level", 1) or 1) + 1
        elif label == DocItemLabel.LIST_ITEM:
            kind = BlockKind.LIST_ITEM
            marker = getattr(item, "marker", "") or ""
            text = f"{marker} {text}".strip() if marker and not text.startswith(marker) else text
        elif label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER):
            kind = BlockKind.HEADER_FOOTER
        elif label == DocItemLabel.CAPTION:
            kind = BlockKind.CAPTION
        elif label == DocItemLabel.CODE:
            kind = BlockKind.CODE
        drafts.append(Draft(kind, text, BlockSource.STRUCTURE, page=page, heading_level=level,
                            bbox=bbox))
    return Structured(drafts)


def merge_with_text_layer(structured: list[Draft], reading: PdfReading
                          ) -> tuple[list[Draft], list[int]]:
    """Docling's drafts, except pages where it lost text; plus hidden text from the layer."""
    pages = sorted({d.page for d in reading.drafts if d.page} | {d.page for d in structured
                                                                  if d.page})
    out: list[Draft] = []
    replaced: list[int] = []
    for page in pages:
        layer = [d for d in reading.drafts if d.page == page]
        mine = [d for d in structured if d.page == page]
        layer_text = " ".join(d.text for d in layer if not d.hidden)
        mine_text = " ".join(d.text for d in mine)
        if layer_text and coverage(layer_text, mine_text) < _KEEP:
            out.extend(layer)
            replaced.append(page)
        else:
            out.extend(mine)
            out.extend(d for d in layer if d.hidden)
    return out, replaced
