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
