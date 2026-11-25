"""Run settings from environment variables and the optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

PROFILES = ("quick", "thorough")
ROLES = ("analyst", "judge", "opponent", "vision")
# A role without its own model borrows this role's chain.
ROLE_FALLBACK = {"analyst": "judge", "opponent": "judge", "vision": "judge"}

# Model request budget per run when REQAUDIT_MAX_LLM_CALLS is not set.
_DEFAULT_BUDGET = {"quick": 60, "thorough": 400}

# Which environment variable holds the key for each provider. Ollama needs none.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
}

# Used when no model is configured: the first ones whose key is present, in this order.
# gpt-5.5 was chosen by a live comparison on the organisers' document (2026-11-26).
# The Gemini default has not been tried live yet.
AUTO_MODELS = ("openai:gpt-5.5", "gemini:gemini-3.5-flash-lite")


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
    # Optional ceiling on the run's catalogue cost in USD; None means no ceiling.
    max_cost_usd: float | None = None
    requests_per_minute: float | None = None
    request_timeout_s: float = 180.0
    max_attempts: int = 4
    backoff_base_s: float = 2.0
    output_retries: int = 2
    # Characters of rendered document per model call; longer documents are split by sections.
    window_chars: int = 40_000
    # Model calls running at the same time within one stage.
    concurrency: int = 6
    # Vision: "auto" reads scanned pages and pictures; "all" also re-reads every PDF page by
    # image and replaces a text layer that lost text; "off" never sends images.
    vision: str = "auto"
    max_vision_pages: int = 40
    max_pictures: int = 40
    # PDF reader: "pymupdf" (text layer), "docling" (structure; needs the docling extra),
    # "auto" uses Docling when it is installed and the profile is thorough.
    pdf_reader: str = "auto"
    # Decisions taken while reading the configuration, shown in the console and the passport.
    notes: list[str] = field(default_factory=list)

    def chain_for(self, role: str) -> list[str]:
        return self.models.get(role) or self.models.get(ROLE_FALLBACK.get(role, ""), [])


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def has_key(model_id: str) -> bool:
    provider = model_id.partition(":")[0]
    keys = PROVIDER_KEYS.get(provider)
    return keys is None or any(_env(k) for k in keys)


def _resolve_models(notes: list[str]) -> dict[str, list[str]]:
    """Configured chains minus models without a key; an automatic choice if nothing is left."""
    models: dict[str, list[str]] = {}
    for role in ROLES:
        configured = parse_chain(_env(f"REQAUDIT_MODEL_{role.upper()}"))
        usable = [m for m in configured if has_key(m)]
        for missing in (m for m in configured if m not in usable):
            keys = " или ".join(PROVIDER_KEYS[missing.partition(':')[0]])
            notes.append(f"Модель {missing} (роль {role}) пропущена: не задан {keys}.")
        if usable:
            models[role] = usable

    if "judge" not in models:
        auto = [m for m in AUTO_MODELS if has_key(m)]
        if auto:
            models["judge"] = auto
            notes.append(f"Модель выбрана автоматически по найденному ключу API: {auto[0]}"
                         + (f", запасная {', '.join(auto[1:])}." if auto[1:] else "."))
        else:
            notes.append("Не найден ключ модели: задайте OPENAI_API_KEY или GEMINI_API_KEY "
                         "в файле .env. Без модели агент прочитает документ, но не проверит его.")
    return models


def _choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    value = (_env(name) or default).lower()
    if value not in allowed:
        raise ValueError(f"{name}={value}: допустимо {', '.join(allowed)}.")
    return value


def parse_chain(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def load_settings(
    profile: str | None = None,
    runs_dir: Path | None = None,
    cache_read: bool = True,
) -> Settings:
    # Search from the working directory, not from this file: an installed package lives in
    # site-packages, while the user's .env sits where they run the command.
    load_dotenv(find_dotenv(usecwd=True), override=False)

    chosen = profile or _env("REQAUDIT_PROFILE") or "quick"
    if chosen not in PROFILES:
        raise ValueError(f"Неизвестный профиль '{chosen}'. Допустимо: {', '.join(PROFILES)}.")

    notes: list[str] = []
    models = _resolve_models(notes)
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
        max_cost_usd=float(cost) if (cost := _env("REQAUDIT_MAX_COST_USD")) else None,
        concurrency=max(1, int(_env("REQAUDIT_CONCURRENCY") or 6)),
        vision=_choice("REQAUDIT_VISION", ("auto", "all", "off"),
                       "all" if chosen == "thorough" else "auto"),
        pdf_reader=_choice("REQAUDIT_PDF_READER", ("auto", "pymupdf", "docling"), "auto"),
        notes=notes,
    )
