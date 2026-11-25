"""Prompt files: loading, rendering and the fingerprint that goes into the run passport.

Prompts live in `reqaudit/prompts/*.md` with a small front matter block:

    ---
    id: system_base
    version: 1
    ---
    Prompt text with {{placeholders}}.

Placeholders use double braces so that JSON examples inside prompts stay intact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    body: str
    path: Path


def load(name: str) -> Prompt:
    path = PROMPTS_DIR / f"{name}.md"
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        _, header, body = raw.split("---", 2)
        for line in header.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return Prompt(id=meta.get("id", name), version=meta.get("version", "0"),
                  body=body.strip() + "\n", path=path)


def render(name: str, /, **values: str) -> str:
    prompt = load(name)

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Prompt '{name}' needs a value for '{key}'")
        return values[key]

    return _PLACEHOLDER.sub(substitute, prompt.body)


def compose(*names: str, **values: str) -> tuple[str, list[str]]:
    """Join `system_base` and role prompts into one instruction text.

    Returns the text and the prompt ids with versions, for the trace.
    """
    parts, ids = [], []
    for name in ("system_base", *names):
        prompt = load(name)
        parts.append(render(name, **values).strip())
        ids.append(f"{prompt.id}@{prompt.version}")
    return "\n\n".join(parts) + "\n", ids


def fingerprint() -> str:
    """Hash of every prompt file; changes whenever any prompt changes."""
    digest = hashlib.sha256()
    for path in sorted(PROMPTS_DIR.rglob("*.md")):
        if path.name == "README.md":
            continue
        digest.update(path.relative_to(PROMPTS_DIR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]
