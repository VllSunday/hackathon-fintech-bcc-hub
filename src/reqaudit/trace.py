"""Append-only trace of a run: one JSON object per line in runs/<id>/trace.jsonl."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, /, **fields: Any) -> None:
        record = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
