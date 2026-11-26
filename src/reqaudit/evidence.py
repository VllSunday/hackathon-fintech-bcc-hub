"""Quote matching: does a quote from the model really exist in the given blocks?

Used by segmentation (s2) and the evidence gate (s7). Matching is exact on a
normalised key, never fuzzy: a quote with a changed number or word does not match.

The key ignores what a model legitimately drops or alters when copying text:
markdown markup, table pipes, letter case, kinds of dashes and quotation marks,
`ё`/`е`, and runs of whitespace.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from reqaudit.models import Block

_MARKUP = re.compile(r"[*_`#>|]+")
_DASHES = re.compile("[‐-―−]")
_QUOTES = re.compile("[\"'«»„“”‘’‚]")
_SPACES = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\.\.\.|…")

MIN_KEY_LENGTH = 8


def match_key(text: str) -> str:
    key = unicodedata.normalize("NFKC", text)
    key = key.replace("ё", "е").replace("Ё", "Е")
    key = _DASHES.sub("-", key)
    key = _QUOTES.sub('"', key)
    key = _MARKUP.sub(" ", key)
    return _SPACES.sub(" ", key).strip().casefold()


def has_ellipsis(quote: str) -> bool:
    return bool(_ELLIPSIS.search(quote))


def blocks_key(blocks: Iterable[Block]) -> str:
    return " ".join(match_key(b.text) for b in blocks)


def quote_in(quote: str, blocks: Iterable[Block]) -> bool:
    """True when the whole quote occurs in the blocks, read in the given order."""
    key = match_key(quote)
    return len(key) >= MIN_KEY_LENGTH and key in blocks_key(blocks)


def locate(quote: str, blocks: Iterable[Block]) -> list[str]:
    """Ids of single blocks that contain the quote; used to correct a wrong block id."""
    key = match_key(quote)
    if len(key) < MIN_KEY_LENGTH:
        return []
    return [b.id for b in blocks if key in match_key(b.text)]


# --- where a quote stands inside its block (for highlighting) ---------------------------


def match_key_with_index(text: str) -> tuple[str, list[int]]:
    """`match_key` of the text, with the source index of every character of the key."""
    chars: list[str] = []
    index: list[int] = []
    for i, source in enumerate(text):
        for ch in unicodedata.normalize("NFKC", source):
            if ch in "ёЁ":
                ch = "е" if ch == "ё" else "Е"
            if _DASHES.match(ch):
                ch = "-"
            elif _QUOTES.match(ch):
                ch = '"'
            elif _MARKUP.fullmatch(ch):
                ch = " "
            for folded in ch.casefold():
                if folded.isspace():
                    if not chars or chars[-1] == " ":
                        continue
                    folded = " "
                chars.append(folded)
                index.append(i)
    while chars and chars[-1] == " ":
        chars.pop()
        index.pop()
    return "".join(chars), index


def quote_span(quote: str, block_text: str) -> tuple[int, int] | None:
    """Character range of the quote in the block text, by the same key the gate matches on.

    None when the range cannot be told; the caller then marks the whole block.
    """
    key = match_key(quote)
    if len(key) < MIN_KEY_LENGTH:
        return None
    stream, index = match_key_with_index(block_text)
    if stream != match_key(block_text):
        return None  # the per-character key drifted from the gate's key; do not guess
    at = stream.find(key)
    if at < 0:
        return None
    return index[at], index[at + len(key) - 1] + 1
