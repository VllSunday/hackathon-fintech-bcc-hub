"""Output checks that run inside the model call and send a concrete complaint back."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LETTER = re.compile(r"[^\W\d_]")

Check = Callable[[Any], str | None]


def _values(obj: Any, path: list[str]) -> Iterable[tuple[str, Any]]:
    """Yield (location, value) for a dotted path; lists are expanded with their index."""
    if not path:
        yield "", obj
        return
    head, rest = path[0], path[1:]
    value = getattr(obj, head, None)
    if isinstance(value, list):
        for i, item in enumerate(value):
            for loc, v in _values(item, rest):
                yield f"{head}[{i}]{'.' + loc if loc else ''}", v
    else:
        for loc, v in _values(value, rest):
            yield f"{head}{'.' + loc if loc else ''}", v


def require_russian(*fields: str) -> Check:
    """Free-text fields must be Russian. A field with letters but no Cyrillic fails."""

    def check(output: BaseModel) -> str | None:
        bad = []
        for field in fields:
            for location, value in _values(output, field.split(".")):
                if isinstance(value, str) and _LETTER.search(value) and not _CYRILLIC.search(value):
                    bad.append(f"{location}: {value[:80]!r}")
        if bad:
            return ("These free-text fields must be written in Russian. Rewrite them in Russian, "
                    "keep everything else unchanged: " + "; ".join(bad))
        return None

    return check


def combine(*checks: Check | None) -> Check | None:
    active = [c for c in checks if c is not None]
    if not active:
        return None

    def check(output: Any) -> str | None:
        messages = [m for c in active if (m := c(output))]
        return "\n".join(messages) or None

    return check
