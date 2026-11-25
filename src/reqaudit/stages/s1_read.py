"""s1 Reading: turn the accepted file into blocks with anchors, whatever its format.

| Format   | Reader                                   | Vision                          |
|----------|------------------------------------------|---------------------------------|
| Markdown | line-based reader                        | —                               |
| Text     | line-based reader                        | —                               |
| HTML     | to marked-up lines, then line reader     | —                               |
| RTF      | striprtf, then line reader               | —                               |
| DOCX     | XML in document order                    | pictures                        |
| PDF      | PyMuPDF text layer, or Docling           | scanned or garbled pages,       |
|          |                                          | pictures, cross-check in "all"  |

Vision results are blocks with source `vlm`; a page or picture vision could not read is
reported as an issue, never silently dropped.
"""

from __future__ import annotations

from pathlib import Path

from reqaudit.llm.client import LLMError
from reqaudit.models import BlockKind, BlockSource, DocumentIR, ReadIssue, SourceFormat
from reqaudit.reading import office, vision
from reqaudit.reading.assemble import Draft, finalize
from reqaudit.reading.pdf import PdfReading, read_pdf
from reqaudit.reading.text import read_text_document
from reqaudit.stages.base import RunContext, Stage

_LOSSY = 0.85  # below this word coverage the text layer is taken as lossy


class ReaderNotImplemented(Exception):
    pass


def _context_around(drafts: list[Draft], index: int) -> str:
    near = drafts[max(0, index - 3):index + 2]
    return "\n".join(d.text for d in near if not d.hidden)


def _insert_pictures(drafts: list[Draft], pictures: list[vision.Picture],
                     results: list, issues: list[ReadIssue]) -> list[Draft]:
    """Put picture descriptions into reading order; report the ones that failed."""
    placed: list[tuple[int, Draft]] = []
    skipped = 0
    for picture, result in zip(pictures, results, strict=True):
        where = f"стр. {picture.page}" if picture.page else "в документе"
        if isinstance(result, LLMError):
            issues.append(ReadIssue(kind="picture_unread",
                                    message=f"Изображение ({where}) не прочитано: "
                                            f"{result.message}"))
            continue
        if result.kind in ("logo", "decorative"):
            skipped += 1
            continue
        placed.append((picture.position, Draft(
            BlockKind.IMAGE_TEXT, vision.picture_text(result), BlockSource.VLM,
            page=picture.page, confidence=vision.VLM_CONFIDENCE)))
    out = list(drafts)
    for position, draft in sorted(placed, key=lambda p: p[0], reverse=True):
        out.insert(position, draft)
    return out


class ReadStage(Stage):
    code = "s1"
    title = "Чтение"
    artifact = "s1_blocks.json"

    def run(self, ctx: RunContext) -> DocumentIR:
        intake = ctx.state.intake
        assert intake is not None, "s1 needs s0"
        fmt = intake.detected_format
        path = Path(intake.path)

        if fmt in (SourceFormat.MARKDOWN, SourceFormat.TEXT) and intake.text is not None:
            document = read_text_document(intake.text, fmt)
        elif fmt == SourceFormat.HTML and intake.text is not None:
            document = office.read_html(intake.text)
        elif fmt == SourceFormat.RTF:
            document = office.read_rtf(path.read_bytes())
        elif fmt == SourceFormat.DOCX:
            document = self._docx(ctx, path)
        elif fmt == SourceFormat.PDF:
            document = self._pdf(ctx, path)
        else:
            raise ReaderNotImplemented(f"Чтение формата {fmt.value} не поддерживается.")

        ctx.state.document = document
        ctx.trace.event("read_summary", stage=self.code, blocks=len(document.blocks),
                        reader=document.reader_chain, issues=[i.kind for i in document.issues])
        return document

    # --- DOCX ---------------------------------------------------------------------------

    def _docx(self, ctx: RunContext, path: Path) -> DocumentIR:
        drafts, found, warnings = office.read_docx(path)
        issues: list[ReadIssue] = []
        chain = ["docx_xml"]
        pictures = [vision.Picture(png, None, pos, _context_around(drafts, pos))
                    for pos, png in found[:ctx.settings.max_pictures]]
        if len(found) > len(pictures):
            warnings.append(f"Описаны первые {len(pictures)} изображений из {len(found)}.")
        if pictures and ctx.settings.vision != "off":
            _, results = vision.run_vision(ctx.llm, {}, pictures)
            drafts = _insert_pictures(drafts, pictures, results, issues)
            chain.append("vision")
        elif pictures:
            issues.append(ReadIssue(kind="pictures_skipped",
                                    message=f"В документе {len(pictures)} изображений; они не "
                                            "прочитаны, потому что зрение отключено."))
        blocks, block_issues = finalize(drafts)
        return DocumentIR(title=_title(blocks), format=SourceFormat.DOCX, blocks=blocks,
                          reader_chain=chain, issues=[*block_issues, *issues],
                          warnings=warnings)

    # --- PDF ----------------------------------------------------------------------------

    def _pdf(self, ctx: RunContext, path: Path) -> DocumentIR:
        settings = ctx.settings
        reading = read_pdf(path)
        chain = ["pymupdf"]
        drafts = reading.drafts
        warnings = list(reading.warnings)
        issues: list[ReadIssue] = []

        from reqaudit.reading import docling_reader
        use_docling = settings.pdf_reader == "docling" or (
            settings.pdf_reader == "auto" and settings.profile == "thorough"
            and docling_reader.available())
        if use_docling:
            structured = docling_reader.read(path)
            if structured.error:
                warnings.append(structured.error)
            else:
                drafts, replaced = docling_reader.merge_with_text_layer(structured.drafts,
                                                                       reading)
                chain = ["docling", "pymupdf (сверка)"]
                if replaced:
                    warnings.append("Docling потерял часть текста на страницах "
                                    f"{', '.join(map(str, replaced))}; для них взят текстовый "
                                    "слой.")

        if settings.vision == "off":
            for report in reading.pages:
                if report.needs_vision:
                    issues.append(ReadIssue(kind="page_unread", message=(
                        f"Страница {report.page} не прочитана ({report.reason}); зрение "
                        "отключено.")))
            blocks, block_issues = finalize(drafts)
            return DocumentIR(title=_title(blocks), format=SourceFormat.PDF, blocks=blocks,
                              reader_chain=chain, issues=[*block_issues, *issues],
                              warnings=warnings)

        drafts, issues = self._pdf_vision(ctx, path, reading, drafts, warnings)
        chain.append("vision")
        blocks, block_issues = finalize(drafts)
        return DocumentIR(title=_title(blocks), format=SourceFormat.PDF, blocks=blocks,
                          reader_chain=chain, issues=[*block_issues, *issues],
                          warnings=warnings)

    def _pdf_vision(self, ctx: RunContext, path: Path, reading: PdfReading,
                    drafts: list[Draft], warnings: list[str]
                    ) -> tuple[list[Draft], list[ReadIssue]]:
        settings = ctx.settings
        issues: list[ReadIssue] = []
        need = {r.page: r.reason for r in reading.pages if r.needs_vision}
        pages = dict(reading.page_pngs)
        if settings.vision == "all":
            import pymupdf
            doc = pymupdf.open(path)
            if doc.needs_pass:
                doc.authenticate("")
            for report in reading.pages:
                if report.page not in pages and report.chars > 0:
                    pages[report.page] = doc[report.page - 1].get_pixmap(dpi=150).tobytes("png")
        if len(pages) > settings.max_vision_pages:
            keep = sorted(pages)[:settings.max_vision_pages]
            warnings.append(f"Зрением прочитаны первые {len(keep)} страниц из {len(pages)}.")
            pages = {n: pages[n] for n in keep}

        pictures = []
        for image in reading.images:
            if image.page in need or len(pictures) >= settings.max_pictures:
                continue
            position = next((i for i, d in enumerate(drafts)
                             if (d.page or 0) > image.page
                             or (d.page == image.page and d.bbox and d.bbox[1] > image.order)),
                            len(drafts))
            pictures.append(vision.Picture(image.png, image.page, position,
                                           _context_around(drafts, position)))

        transcripts, descriptions = vision.run_vision(ctx.llm, pages, pictures)
        drafts = _insert_pictures(drafts, pictures, descriptions, issues)

        replace: dict[int, list[Draft]] = {}
        for page, result in transcripts.items():
            if isinstance(result, LLMError):
                if page in need:
                    issues.append(ReadIssue(kind="page_unread", message=(
                        f"Страница {page} не прочитана ({need[page]}): {result.message}")))
                continue
            transcribed = vision.transcript_drafts(result, page)
            if page in need:
                replace[page] = transcribed
                issues.append(ReadIssue(kind="page_by_vision", message=(
                    f"Страница {page} прочитана по изображению ({need[page]}); цитаты с неё "
                    "взяты из распознанного текста.")))
                continue
            layer = " ".join(d.text for d in drafts if d.page == page and not d.hidden)
            seen = " ".join(d.text for d in transcribed)
            share = vision.coverage(seen, layer)
            if share < _LOSSY:
                replace[page] = transcribed
                issues.append(ReadIssue(kind="text_layer_lossy", message=(
                    f"На странице {page} текстовый слой потерял часть текста: в нём найдено "
                    f"{share:.0%} слов, видимых на изображении. Страница прочитана по "
                    "изображению.")))

        out: list[Draft] = []
        done: set[int] = set()
        for draft in drafts:
            page = draft.page or 0
            if page in replace:
                if page not in done:
                    out.extend(replace[page])
                    done.add(page)
                if draft.hidden:
                    out.append(draft)  # hidden text stays visible to the checks
                continue
            out.append(draft)
        for page in sorted(set(replace) - done):
            index = next((i for i, d in enumerate(out) if (d.page or 0) > page), len(out))
            out[index:index] = replace[page]
        return out, issues


def _title(blocks) -> str | None:
    return next((b.text for b in blocks if b.kind == BlockKind.HEADING and not b.hidden), None)
