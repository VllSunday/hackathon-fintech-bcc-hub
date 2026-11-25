"""s1 Reading: turn the accepted file into blocks with anchors.

Implemented: Markdown and plain text. PDF, DOCX, HTML and RTF readers come next;
until then the stage fails with a clear message and the report says so.
"""

from __future__ import annotations

from reqaudit.models import DocumentIR, SourceFormat
from reqaudit.reading.text import read_text_document
from reqaudit.stages.base import RunContext, Stage

_TEXT_FORMATS = {SourceFormat.MARKDOWN, SourceFormat.TEXT}


class ReaderNotImplemented(Exception):
    pass


class ReadStage(Stage):
    code = "s1"
    title = "Чтение"
    artifact = "s1_blocks.json"

    def run(self, ctx: RunContext) -> DocumentIR:
        intake = ctx.state.intake
        assert intake is not None, "s1 needs s0"

        if intake.detected_format in _TEXT_FORMATS and intake.text is not None:
            document = read_text_document(intake.text, intake.detected_format)
        else:
            raise ReaderNotImplemented(
                f"Чтение формата {intake.detected_format.value} ещё не реализовано. "
                "Сейчас поддерживаются Markdown и обычный текст.")

        ctx.state.document = document
        ctx.trace.event("read_summary", stage=self.code, blocks=len(document.blocks),
                        issues=[i.kind for i in document.issues])
        return document
