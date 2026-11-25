"""Text clean-up shared by readers: line endings, invisible characters, double encoding."""

from __future__ import annotations

import re

# Characters that render as nothing but break quote matching and can hide text.
_INVISIBLE = re.compile("[­​‌‍⁠﻿]")

# UTF-8 bytes shown through a single-byte code page: "РџСЂРё" (cp1251) or "Ð¢Ð¸Ð¿" (latin-1).
_MOJIBAKE_CP1251 = re.compile("[РС][\u0080-¿Ђ-Џђ-џ‘-›№]")
_MOJIBAKE_LATIN1 = re.compile("[ÐÑ][\u0080-¿]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_invisible(text: str) -> tuple[str, int]:
    cleaned, count = _INVISIBLE.subn("", text)
    return cleaned, count


def _cyrillic_share(text: str) -> float:
    letters = sum(1 for ch in text if ch.isalpha())
    return len(_CYRILLIC.findall(text)) / letters if letters else 0.0


def mojibake_hits(text: str) -> int:
    return len(_MOJIBAKE_CP1251.findall(text)) + len(_MOJIBAKE_LATIN1.findall(text))


def repair_mojibake(text: str) -> tuple[str, str | None]:
    """Undo UTF-8 text that was decoded with the wrong single-byte code page.

    Returns (text, method). Method is None when nothing was changed. The repair is kept
    only if it decodes cleanly and leaves more Cyrillic than before.
    """
    for pattern, codec in ((_MOJIBAKE_CP1251, "cp1251"), (_MOJIBAKE_LATIN1, "latin-1")):
        hits = len(pattern.findall(text))
        if hits < 5 or hits < len(text) // 200:
            continue
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if _cyrillic_share(fixed) > _cyrillic_share(text):
            return fixed, codec
    return text, None
