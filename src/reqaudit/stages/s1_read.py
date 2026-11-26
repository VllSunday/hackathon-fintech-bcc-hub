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

from reqaudit.errors import InputError
from reqaudit.llm.client import LLMError
from reqaudit.models import BlockKind, BlockSource, DocumentIR, ReadIssue, SourceFormat
from reqaudit.reading import office, vision
from reqaudit.reading.assemble import Draft, finalize
from reqaudit.reading.pdf import PdfReading, read_pdf, render_pages
from reqaudit.reading.text import read_text_document
from reqaudit.stages.base import RunContext, Stage

_LOSSY = 0.85  # below this word coverage the text layer is taken as lossy


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
            raise InputError("format_unsupported",
                             f"Чтение формата {fmt.value} не поддерживается.")

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
            _, results, _ = vision.run_vision(ctx.llm, {}, pictures)
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
            pages |= render_pages(path, [r.page for r in reading.pages
                                         if r.page not in pages and r.chars > 0])
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

        # Pages with no usable text layer are read a second time, by another strategy.
        second = ({n: png for n, png in pages.items() if n in need}
                  if settings.scan_reads >= 2 else {})
        ctx.llm.plan_calls(len(pages) + len(second) + len(pictures), "чтение зрением")
        transcripts, descriptions, rereads = vision.run_vision(ctx.llm, pages, pictures,
                                                               second)
        drafts = _insert_pictures(drafts, pictures, descriptions, issues)

        replace: dict[int, list[Draft]] = {}
        for page, result in transcripts.items():
            reread = rereads.get(page)
            if isinstance(result, LLMError):
                if page in need and isinstance(reread, vision.PageLines) and reread.lines:
                    replace[page] = vision.lines_drafts(reread, page)
                    issues.append(ReadIssue(kind="page_by_vision", message=(
                        f"Страница {page} прочитана по изображению ({need[page]}) только "
                        f"построчно: основное прочтение не удалось ({result.message}).")))
                elif page in need:
                    issues.append(ReadIssue(kind="page_unread", message=(
                        f"Страница {page} не прочитана ({need[page]}): {result.message}")))
                continue
            transcribed = vision.transcript_drafts(result, page)
            if page in need:
                replace[page] = transcribed
                issues.append(ReadIssue(kind="page_by_vision", message=(
                    f"Страница {page} прочитана по изображению ({need[page]}); цитаты с неё "
                    "взяты из распознанного текста.")))
                issues.extend(_compare_readings(page, transcribed, reread))
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


def _compare_readings(page: int, transcribed: list[Draft],
                      reread: vision.PageLines | LLMError | None) -> list[ReadIssue]:
    """Mark where the second reading of a scanned page disagrees with the first."""
    if reread is None:
        return []
    if isinstance(reread, LLMError):
        return [ReadIssue(kind="scan_single_reading", message=(
            f"Страница {page} прочитана один раз: второе прочтение не удалось "
            f"({reread.message}), расхождения не проверены."))]
    found = vision.discrepancies(" ".join(d.text for d in transcribed),
                                 "\n".join(reread.lines))
    if not found:
        return []
    if len(found) > vision.MAX_DISCREPANCIES:
        for d in transcribed:
            d.confidence = min(d.confidence, vision.DOUBT_CONFIDENCE)
        return [ReadIssue(kind="scan_unreliable", message=(
            f"Страница {page}: два прочтения скана расходятся в {len(found)} местах, текст "
            "страницы ненадёжен; цитаты с неё стоит сверить с оригиналом."))]
    unplaced = vision.mark_doubts(transcribed, found)
    shown = "; ".join(str(d) for d in found[:5]) + ("; …" if len(found) > 5 else "")
    issues = [ReadIssue(kind="scan_discrepancy_summary", message=(
        f"Страница {page}: два независимых прочтения скана расходятся в {len(found)} "
        f"местах (первое / второе): {shown}. Блоки с расхождениями помечены, цитаты из них "
        "стоит сверить с оригиналом."))]
    if unplaced:
        issues.append(ReadIssue(kind="scan_discrepancy", message=(
            f"Страница {page}: текст есть только во втором прочтении: "
            + "; ".join(str(d) for d in unplaced[:5]) + ".")))
    return issues


def _title(blocks) -> str | None:
    return next((b.text for b in blocks if b.kind == BlockKind.HEADING and not b.hidden), None)
