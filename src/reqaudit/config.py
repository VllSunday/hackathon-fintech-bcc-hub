"""Run settings from environment variables and the optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROFILES = ("quick", "thorough")
ROLES = ("judge", "opponent", "vision")

# Model request budget per run when REQAUDIT_MAX_LLM_CALLS is not set.
_DEFAULT_BUDGET = {"quick": 60, "thorough": 400}


@dataclass(frozen=True)
class Settings:
    profile: str = "quick"
    runs_dir: Path = Path("runs")
    max_input_mb: int = 50
    # Role -> ordered model chain, "provider:model". The first model is primary, the rest
    # are fallbacks tried when it is unavailable.
    models: dict[str, list[str]] = field(default_factory=dict)
    cache_dir: Path = Path(".cache/llm")
    cache_read: bool = True
    max_llm_calls: int = 60
    requests_per_minute: float | None = None
    request_timeout_s: float = 180.0
    max_attempts: int = 4
    backoff_base_s: float = 2.0
    output_retries: int = 2


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def parse_chain(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def load_settings(
    profile: str | None = None,
    runs_dir: Path | None = None,
    cache_read: bool = True,
) -> Settings:
    load_dotenv(override=False)

    chosen = profile or _env("REQAUDIT_PROFILE") or "quick"
    if chosen not in PROFILES:
        raise ValueError(f"Неизвестный профиль '{chosen}'. Допустимо: {', '.join(PROFILES)}.")

    models = {
        role: chain
        for role in ROLES
        if (chain := parse_chain(_env(f"REQAUDIT_MODEL_{role.upper()}")))
    }
    rpm = _env("REQAUDIT_RPM")

    return Settings(
        profile=chosen,
        runs_dir=runs_dir or Path(_env("REQAUDIT_RUNS_DIR") or "runs"),
        max_input_mb=int(_env("REQAUDIT_MAX_INPUT_MB") or 50),
        models=models,
        cache_dir=Path(_env("REQAUDIT_CACHE_DIR") or ".cache/llm"),
        cache_read=cache_read,
        max_llm_calls=int(_env("REQAUDIT_MAX_LLM_CALLS") or _DEFAULT_BUDGET[chosen]),
        requests_per_minute=float(rpm) if rpm else None,
    )
