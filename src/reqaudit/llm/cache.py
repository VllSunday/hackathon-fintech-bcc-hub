"""Disk cache of validated model outputs.

The key covers everything that can change the answer: model, instructions, prompt,
output schema and model settings. A rerun on the same input therefore makes no
requests and produces the same report.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

CACHE_FORMAT = 1


def cache_key(model_id: str, instructions: str, prompt: str, schema: dict[str, Any],
              model_settings: dict[str, Any]) -> str:
    payload = json.dumps(
        {"v": CACHE_FORMAT, "model": model_id, "instructions": instructions, "prompt": prompt,
         "schema": schema, "settings": model_settings},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, key: str, record: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
