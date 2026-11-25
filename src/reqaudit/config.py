"""Run settings from environment variables and the optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROFILES = ("quick", "thorough")


@dataclass(frozen=True)
class Settings:
    profile: str = "quick"
    runs_dir: Path = Path("runs")
    max_input_mb: int = 50
    models: dict[str, str] = field(default_factory=dict)


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_settings(profile: str | None = None, runs_dir: Path | None = None) -> Settings:
    load_dotenv(override=False)

    chosen = profile or _env("REQAUDIT_PROFILE") or "quick"
    if chosen not in PROFILES:
        raise ValueError(f"Неизвестный профиль '{chosen}'. Допустимо: {', '.join(PROFILES)}.")

    models = {
        role: value
        for role in ("judge", "opponent", "vision")
        if (value := _env(f"REQAUDIT_MODEL_{role.upper()}"))
    }

    return Settings(
        profile=chosen,
        runs_dir=runs_dir or Path(_env("REQAUDIT_RUNS_DIR") or "runs"),
        max_input_mb=int(_env("REQAUDIT_MAX_INPUT_MB") or 50),
        models=models,
    )
