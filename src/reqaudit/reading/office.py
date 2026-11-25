"""Readers for DOCX, HTML and RTF.

DOCX is read from its XML in document order, so tables stay where they are, text deleted
in tracked changes is ignored, and hidden text is kept apart as hidden blocks. Pictures
are handed to vision. HTML and RTF are turned into simple marked-up lines and read by the
Markdown reader, which already handles broken structure and hidden comments.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup, Comment
from docx import Document
from docx.oxml.ns import qn
from striprtf.striprtf import rtf_to_text

from reqaudit.errors import InputError
from reqaudit.models import BlockKind, BlockSource, DocumentIR, SourceFormat
from reqaudit.reading.assemble import Draft, table_row
from reqaudit.reading.text import read_text_document

_MAX_UNPACKED = 200 * 1024 * 1024
_MAX_ENTRIES = 10_000
_MAX_RATIO = 200
_HEADING_STYLE = re.compile(r"^(?:heading|заголовок)\s*(\d)$", re.IGNORECASE)
_ANSICPG = re.compile(rb"\\ansicpg(\d+)")


# --- DOCX -------------------------------------------------------------------------------


def _check_archive(path: Path) -> None:
    """Refuse zip bombs before python-docx unpacks anything."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as err:
        raise InputError("docx_broken", "Файл DOCX повреждён: архив не читается.") from err
    total = sum(e.file_size for e in entries)
    packed = sum(e.compress_size for e in entries) or 1
    if len(entries) > _MAX_ENTRIES or total > _MAX_UNPACKED or total / packed > _MAX_RATIO:
        raise InputError("docx_bomb", "Файл DOCX подозрительно сильно сжат или слишком велик "
                                      "после распаковки; он не будет открыт.")


def _run_text(run) -> tuple[str, bool]:
    hidden = run.find(qn("w:rPr")) is not None and run.find(qn("w:rPr")).find(
        qn("w:vanish")) is not None
    parts = []
    for child in run.iter():
        if child.tag == qn("w:t"):
            parts.append(child.text or "")
        elif child.tag in (qn("w:tab"), qn("w:br")):
            parts.append(" ")
    return "".join(parts), hidden


def _paragraph(p) -> tuple[str, str]:
    """Visible and hidden text of a paragraph; deleted text is skipped."""
    visible, hidden = [], []
    for run in p.iter(qn("w:r")):
        if any(a.tag == qn("w:del") for a in run.iterancestors()):
            continue
        text, is_hidden = _run_text(run)
        (hidden if is_hidden else visible).append(text)
    return "".join(visible).strip(), "".join(hidden).strip()


def _style_name(p, styles: dict[str, str]) -> str:
    ppr = p.find(qn("w:pPr"))
    style = ppr.find(qn("w:pStyle")) if ppr is not None else None
    return styles.get(style.get(qn("w:val")), "") if style is not None else ""


def _pictures(element, part) -> list[bytes]:
    """PNG bytes of pictures embedded in an element; formats PyMuPDF cannot open are skipped."""
    out = []
    for blip in element.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid or rid not in part.related_parts:
            continue
        try:
            pix = pymupdf.Pixmap(part.related_parts[rid].blob)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
            out.append(pix.tobytes("png"))
        except Exception:  # noqa: BLE001 - EMF/WMF and the like cannot be converted
            continue
    return out


def read_docx(path: Path) -> tuple[list[Draft], list[tuple[int, bytes]], list[str]]:
    """Drafts in document order, pictures as (draft index, png), and warnings."""
    _check_archive(path)
    try:
        doc = Document(str(path))
    except Exception as err:  # noqa: BLE001
        raise InputError("docx_unreadable", f"Файл DOCX не удалось прочитать: {err}") from err
    styles = {s.style_id: s.name for s in doc.styles}
    drafts: list[Draft] = []
    pictures: list[tuple[int, bytes]] = []
    warnings: list[str] = []
    counter = 0

    for element in doc.element.body.iterchildren():
        if element.tag == qn("w:p"):
            counter += 1
            visible, hidden = _paragraph(element)
            style = _style_name(element, styles)
            heading = _HEADING_STYLE.match(style)
            ppr = element.find(qn("w:pPr"))
            numbered = ppr is not None and ppr.find(qn("w:numPr")) is not None
            if visible:
                if heading or style.lower() == "title":
                    kind, level = BlockKind.HEADING, int(heading.group(1)) if heading else 1
                elif numbered or "list" in style.lower():
                    kind, level = BlockKind.LIST_ITEM, None
                else:
                    kind, level = BlockKind.PARAGRAPH, None
                drafts.append(Draft(kind, visible, BlockSource.STRUCTURE, paragraph=counter,
                                    heading_level=level))
            if hidden:
                drafts.append(Draft(BlockKind.PARAGRAPH, hidden, BlockSource.STRUCTURE,
                                    paragraph=counter, hidden=True,
                                    issues=[("hidden_text", f"Скрытый текст в абзаце {counter}: "
                                                            f"«{hidden[:120]}».")]))
            for png in _pictures(element, doc.part):
                pictures.append((len(drafts), png))
        elif element.tag == qn("w:tbl"):
            header: list[str] | None = None
            for row in element.iter(qn("w:tr")):
                counter += 1
                cells = []
                for cell in row.iter(qn("w:tc")):
                    texts = [_paragraph(p)[0] for p in cell.iter(qn("w:p"))]
                    cells.append(" ".join(t for t in texts if t))
                if header is None:
                    header = cells
                    drafts.append(Draft(BlockKind.TABLE, table_row(cells), BlockSource.STRUCTURE,
                                        paragraph=counter, table_header=header))
                else:
                    drafts.append(Draft(BlockKind.TABLE_ROW, table_row(cells),
                                        BlockSource.STRUCTURE, paragraph=counter,
                                        table_header=header))
            for png in _pictures(element, doc.part):
                pictures.append((len(drafts), png))
    if any(True for _ in doc.element.body.iter(qn("w:ins"))):
        warnings.append("В документе есть неподтверждённые исправления (режим правок); "
                        "проверена версия с ними, удалённый текст не учитывался.")
    return drafts, pictures, warnings


# --- HTML -------------------------------------------------------------------------------

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table", "dt", "dd",
               "blockquote", "figcaption", "caption"]


def _is_hidden(tag) -> bool:
    for node in [tag, *tag.parents]:
        attrs = getattr(node, "attrs", None) or {}
        if "hidden" in attrs or _HIDDEN_STYLE.search(attrs.get("style", "") or ""):
            return True
    return False


def html_to_markup(html: str) -> str:
    """Simple marked-up lines for the Markdown reader; hidden content becomes comments."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "template", "head"]):
        junk.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.replace_with("")
    lines: list[str] = []
    seen: set[int] = set()
    for tag in soup.find_all(_BLOCK_TAGS):
        if any(id(p) in seen for p in tag.parents):
            continue
        seen.add(id(tag))
        if tag.name == "table":
            rows = tag.find_all("tr")
            for n, row in enumerate(rows):
                cells = [" ".join(c.get_text(" ").split()) for c in row.find_all(["th", "td"])]
                lines.append("| " + " | ".join(cells) + " |")
                if n == 0:
                    lines.append("|" + "---|" * len(cells))
            text = None
        elif tag.name == "pre":
            text = "```\n" + tag.get_text() + "\n```"
        else:
            words = " ".join(tag.get_text(" ").split())
            if not words:
                continue
            if tag.name[0] == "h" and tag.name[1:].isdigit():
                text = "#" * int(tag.name[1]) + " " + words
            elif tag.name == "li":
                text = "- " + words
            else:
                text = words
        if text is not None:
            if _is_hidden(tag):
                text = "<!-- " + text.replace("-->", "—>") + " -->"
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


def read_html(html: str) -> DocumentIR:
    document = read_text_document(html_to_markup(html), SourceFormat.MARKDOWN)
    return document.model_copy(update={
        "format": SourceFormat.HTML, "reader_chain": ["html_to_lines", "markdown_lines"],
        "warnings": [*document.warnings, "Номера строк в отчёте относятся к тексту, "
                                         "извлечённому из HTML, а не к исходной разметке."]})


# --- RTF --------------------------------------------------------------------------------


def read_rtf(data: bytes) -> DocumentIR:
    codepage = _ANSICPG.search(data[:4096])
    encoding = f"cp{codepage.group(1).decode()}" if codepage else "cp1251"
    try:
        text = rtf_to_text(data.decode("latin-1"), encoding=encoding, errors="replace")
    except Exception as err:  # noqa: BLE001
        raise InputError("rtf_unreadable", f"Файл RTF не удалось прочитать: {err}") from err
    if not text.strip():
        raise InputError("empty", "В файле RTF нет текста.")
    document = read_text_document(text, SourceFormat.TEXT)
    return document.model_copy(update={"format": SourceFormat.RTF,
                                       "reader_chain": ["striprtf", "plain_text_lines"]})
