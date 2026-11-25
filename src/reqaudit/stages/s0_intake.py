"""s0 Intake: identify the real file type, decode text formats, enforce limits.

Code only, no model calls. The extension is a hint; the bytes decide.
Opening and repairing PDF structure belongs to the s1 readers.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path

from charset_normalizer import from_bytes

from reqaudit.errors import InputError
from reqaudit.models import IntakeResult, SourceFormat
from reqaudit.stages.base import RunContext, Stage

_EXPECTED_EXTENSIONS: dict[SourceFormat, set[str]] = {
    SourceFormat.PDF: {".pdf"},
    SourceFormat.DOCX: {".docx"},
    SourceFormat.MARKDOWN: {".md", ".markdown"},
    SourceFormat.TEXT: {".txt", ".text", ""},
    SourceFormat.HTML: {".html", ".htm"},
    SourceFormat.RTF: {".rtf"},
}

_UNSUPPORTED: dict[SourceFormat, str] = {
    SourceFormat.DOC_LEGACY:
        "Файл в старом формате Word (.doc). Пересохраните его как .docx или PDF.",
    SourceFormat.ZIP_OTHER:
        "Файл является архивом, но не документом Word (.docx), или архив повреждён.",
    SourceFormat.BINARY:
        "Файл не похож ни на один поддерживаемый формат: PDF, DOCX, MD, TXT, HTML, RTF.",
}

_MARKDOWN_LINE = re.compile(r"^(#{1,6}\s|\s*[-*+]\s|\s*\d+\.\s|\|.*\|\s*$|```)", re.MULTILINE)
_HTML_START = re.compile(r"<(!doctype\s+html|html[\s>]|body[\s>]|head[\s>])", re.IGNORECASE)
_CYRILLIC = re.compile(r"[А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]")


def sniff_binary(data: bytes) -> SourceFormat | None:
    """Formats recognisable by signature. None means text-like."""
    if b"%PDF-" in data[:1024]:
        return SourceFormat.PDF
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            return SourceFormat.ZIP_OTHER
        return SourceFormat.DOCX if "word/document.xml" in names else SourceFormat.ZIP_OTHER
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return SourceFormat.DOC_LEGACY
    if data.lstrip()[:5] == b"{\\rtf":
        return SourceFormat.RTF
    if not data.startswith((b"\xff\xfe", b"\xfe\xff")) and b"\x00" in data[:8192]:
        return SourceFormat.BINARY
    return None


def _cyrillic_share(text: str) -> float:
    letters = sum(1 for ch in text if ch.isalpha())
    return len(_CYRILLIC.findall(text)) / letters if letters else 0.0


def decode_text(data: bytes) -> tuple[str, str, list[str]]:
    """Return (text, encoding, warnings)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig", []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16", []
    try:
        return data.decode("utf-8"), "utf-8", []
    except UnicodeDecodeError:
        pass

    candidates: list[tuple[str, str]] = []
    best = from_bytes(data).best()
    if best is not None and best.encoding:
        candidates.append((str(best), best.encoding))
    candidates.append((data.decode("cp1251", errors="replace"), "cp1251"))

    # Short Cyrillic texts are often misdetected; prefer the reading with more Cyrillic letters.
    text, encoding = max(candidates, key=lambda c: _cyrillic_share(c[0]))
    return text, encoding, [f"Файл не в UTF-8, кодировка определена автоматически: {encoding}."]


def classify_text(text: str, extension: str) -> SourceFormat:
    head = text[:4096]
    if _HTML_START.search(head):
        return SourceFormat.HTML
    if extension in _EXPECTED_EXTENSIONS[SourceFormat.MARKDOWN]:
        return SourceFormat.MARKDOWN
    if len(_MARKDOWN_LINE.findall(text)) >= 3:
        return SourceFormat.MARKDOWN
    return SourceFormat.TEXT


def inspect_file(path: Path, max_input_mb: int) -> IntakeResult:
    if not path.exists():
        raise InputError("not_found", f"Файл не найден: {path}")
    if not path.is_file():
        raise InputError("not_a_file", f"Указанный путь не является файлом: {path}")

    size = path.stat().st_size
    if size == 0:
        raise InputError("empty", "Файл пустой: в нём нет ни одного байта.")
    if size > max_input_mb * 1024 * 1024:
        raise InputError(
            "too_large",
            f"Файл больше допустимых {max_input_mb} МБ. Размер можно поднять переменной "
            "REQAUDIT_MAX_INPUT_MB.",
        )

    data = path.read_bytes()
    extension = path.suffix.lower()
    warnings: list[str] = []
    encoding: str | None = None
    text: str | None = None

    detected = sniff_binary(data)
    if detected in _UNSUPPORTED:
        raise InputError(f"unsupported_{detected}", _UNSUPPORTED[detected])

    if detected is None:
        text, encoding, decode_warnings = decode_text(data)
        warnings.extend(decode_warnings)
        if not text.strip():
            raise InputError("empty", "Файл содержит только пробельные символы.")
        if text.count("�") > max(3, len(text) // 200):
            warnings.append("В тексте много нераспознанных символов: возможна неверная кодировка.")
        detected = classify_text(text, extension)

    mismatch = extension not in _EXPECTED_EXTENSIONS.get(detected, set())
    if mismatch:
        warnings.append(
            f"Расширение «{extension or 'нет'}» не совпадает с содержимым: файл прочитан "
            f"как {detected.value}."
        )

    return IntakeResult(
        path=str(path),
        file_name=path.name,
        extension=extension,
        size_bytes=size,
        sha256=hashlib.sha256(data).hexdigest(),
        detected_format=detected,
        extension_mismatch=mismatch,
        encoding=encoding,
        text=text,
        warnings=warnings,
    )


class IntakeStage(Stage):
    code = "s0"
    title = "Приёмка"
    artifact = "s0_intake.json"

    def run(self, ctx: RunContext) -> IntakeResult:
        result = inspect_file(ctx.input_path, ctx.settings.max_input_mb)
        ctx.state.intake = result
        return result
