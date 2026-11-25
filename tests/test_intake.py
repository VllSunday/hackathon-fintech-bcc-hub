import zipfile
from pathlib import Path

import pytest

from reqaudit.errors import InputError
from reqaudit.models import SourceFormat
from reqaudit.stages.s0_intake import inspect_file

RU = "Держатель карты задаёт порог расходов. Сервис отправляет push при достижении порога."


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_markdown_utf8(tmp_path):
    result = inspect_file(write(tmp_path, "spec.md", f"# ТЗ\n\n{RU}\n".encode()), 50)
    assert result.detected_format == SourceFormat.MARKDOWN
    assert result.encoding == "utf-8"
    assert RU in result.text
    assert not result.extension_mismatch


def test_utf8_bom(tmp_path):
    result = inspect_file(write(tmp_path, "spec.txt", b"\xef\xbb\xbf" + RU.encode()), 50)
    assert result.encoding == "utf-8-sig"
    assert result.text.startswith("Держатель")


def test_cp1251_is_detected(tmp_path):
    result = inspect_file(write(tmp_path, "spec.txt", RU.encode("cp1251")), 50)
    assert result.text == RU
    assert result.warnings


def test_short_cp1251_prefers_cyrillic(tmp_path):
    result = inspect_file(write(tmp_path, "short.txt", "Порог расходов".encode("cp1251")), 50)
    assert result.text == "Порог расходов"


def test_pdf_by_signature_even_with_wrong_extension(tmp_path):
    result = inspect_file(write(tmp_path, "spec.txt", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"), 50)
    assert result.detected_format == SourceFormat.PDF
    assert result.extension_mismatch
    assert result.text is None


def test_pdf_with_junk_before_header(tmp_path):
    result = inspect_file(write(tmp_path, "spec.pdf", b"garbage\r\n%PDF-1.4\n"), 50)
    assert result.detected_format == SourceFormat.PDF


def test_docx_by_content(tmp_path):
    path = tmp_path / "spec.pdf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    result = inspect_file(path, 50)
    assert result.detected_format == SourceFormat.DOCX
    assert result.extension_mismatch


def test_html(tmp_path):
    html = f"<!DOCTYPE html><html><body><p>{RU}</p></body></html>".encode()
    assert inspect_file(write(tmp_path, "spec.html", html), 50).detected_format == SourceFormat.HTML


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("empty.md", b"", "empty"),
        ("blank.md", b"  \n\t\n", "empty"),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "unsupported_doc_legacy"),
        ("junk.pdf", b"\x00\x01\x02\x03" * 100, "unsupported_binary"),
        ("broken.docx", b"PK\x03\x04broken", "unsupported_zip_other"),
    ],
)
def test_rejected_inputs(tmp_path, name, data, code):
    with pytest.raises(InputError) as info:
        inspect_file(write(tmp_path, name, data), 50)
    assert info.value.code == code
    assert info.value.message


def test_missing_file(tmp_path):
    with pytest.raises(InputError) as info:
        inspect_file(tmp_path / "nope.md", 50)
    assert info.value.code == "not_found"


def test_size_limit(tmp_path):
    path = write(tmp_path, "big.txt", b"a" * (1024 * 1024 + 1))
    with pytest.raises(InputError) as info:
        inspect_file(path, 1)
    assert info.value.code == "too_large"
