"""Run settings from environment variables and the optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

PROFILES = ("quick", "thorough")
# Kinds of work, each with its own model chain and thinking depth if configured:
#   analyst   — profile, segmentation, facts (s2, s3): extraction;
#   reviewer  — checks of individual requirements (s5 requirement cells);
#   judge     — checks of the set and of the document (other s5 cells), re-check (s8b);
#   opponent  — tries to refute every finding (s6);
#   arbiter   — decides disputes between reviewer and opponent (s6);
#   editor    — merges duplicates and writes the questions (s8);
#   vision    — reads pages and pictures (s1).
ROLES = ("analyst", "reviewer", "judge", "opponent", "arbiter", "editor", "vision")
# A role without its own model borrows the judge's chain.
ROLE_FALLBACK = {role: "judge" for role in ROLES if role != "judge"}
THINKING_LEVELS = ("minimal", "low", "medium", "high")
# Providers whose prompt cache rewards sending one request of a group first; on the
# others the second wave only waits (Vertex cached 16k of 339k input tokens in one run).
WARM_UP_PROVIDERS = ("openai", "anthropic")

# Model request budget per run when REQAUDIT_MAX_LLM_CALLS is not set.
_DEFAULT_BUDGET = {"quick": 60, "thorough": 400}

# Which environment variable holds the key for each provider. Ollama needs none.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    # Gemini on Google Cloud (Vertex AI express mode): a Cloud API key, usually "AQ.".
    "vertex": ("VERTEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
}

# Used when no model is configured: the first ones whose key is present, in this order.
# gpt-5.5 was chosen by a live comparison on the organisers' document (2026-11-26).
# The Gemini default has not been tried live yet.
AUTO_MODELS = ("openai:gpt-5.5", "gemini:gemini-3.5-flash-lite")
# A Google Cloud key does not work with AI Studio, so its automatic choice is Vertex AI.
AUTO_VERTEX_MODELS = ("vertex:gemini-3.1-pro-preview", "vertex:gemini-3.5-flash")


def provider_key(provider: str) -> tuple[str, str] | None:
    """(variable name, key) of the first key set for the provider, or None."""
    for name in PROVIDER_KEYS.get(provider, ()):
        if value := os.environ.get(name, "").strip():
            return name, value
    return None


def vertex_key() -> str | None:
    """The Google key, if it is a Google Cloud (Vertex AI) key rather than an AI Studio one."""
    found = provider_key("vertex")
    if found is None:
        return None
    name, value = found
    return value if name == "VERTEX_API_KEY" or value.startswith("AQ.") else None


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
    # True when REQAUDIT_MAX_LLM_CALLS is set: the cap is then the user's and stays fixed;
    # otherwise it is a starting value the run raises as it learns the document's size.
    max_llm_calls_fixed: bool = False
    # Optional ceiling on the run's catalogue cost in USD; None means no ceiling.
    max_cost_usd: float | None = None
    requests_per_minute: float | None = None
    request_timeout_s: float = 180.0
    max_attempts: int = 5
    backoff_base_s: float = 4.0
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
    # Marker words (s5): code lists words that often signal a defect, the reviewer decides
    # on each in context. Cells without markers send the same request as with this off.
    markers: bool = True
    # Readings of a page without a text layer: 2 reads it twice by different strategies and
    # marks where the readings differ; 1 reads it once.
    scan_reads: int = 2
    # Decisions taken while reading the configuration, shown in the console and the passport.
    notes: list[str] = field(default_factory=list)
    # Thinking depth per role (REQAUDIT_THINKING_<ROLE>); a role not listed uses the
    # model's own default. Changes what the model is asked, so it is part of the cache key.
    thinking: dict[str, str] = field(default_factory=dict)
    # Prompt-cache warm-up: "auto" only for WARM_UP_PROVIDERS, "on" always, "off" never.
    warm_up: str = "auto"
    # Independent reviewer passes per cell in s5, findings united (REQAUDIT_REVIEW_PASSES).
    review_passes: int = 2

    def chain_for(self, role: str) -> list[str]:
        return self.models.get(role) or self.models.get(ROLE_FALLBACK.get(role, ""), [])


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def has_key(model_id: str) -> bool:
    provider = model_id.partition(":")[0]
    keys = PROVIDER_KEYS.get(provider)
    return keys is None or any(_env(k) for k in keys)


def _with_vertex_fallback(chain: list[str], role: str, notes: list[str]) -> list[str]:
    """With a Google Cloud key, each gemini:X model gets vertex:X as its fallback.

    A Cloud key may or may not be allowed to call AI Studio; when it is not, the provider
    answers 403 and the client moves on to the same model on Vertex AI, so one key works
    either way without editing the configuration.
    """
    if not vertex_key():
        return chain
    extra = [f"vertex:{m.partition(':')[2]}" for m in chain if m.startswith("gemini:")]
    extra = [m for m in extra if m not in chain]
    if extra:
        notes.append(f"Роль {role}: ключ Google Cloud найден, запасные модели через Vertex AI: "
                     f"{', '.join(extra)}.")
    return chain + extra


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
            models[role] = _with_vertex_fallback(usable, role, notes)

    if "judge" not in models:
        auto = [m for m in AUTO_MODELS if has_key(m)]
        if vertex_key():
            auto = [m for m in auto if not m.startswith("gemini:")] + list(AUTO_VERTEX_MODELS)
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
        max_llm_calls_fixed=_env("REQAUDIT_MAX_LLM_CALLS") is not None,
        requests_per_minute=float(rpm) if rpm else None,
        max_cost_usd=float(cost) if (cost := _env("REQAUDIT_MAX_COST_USD")) else None,
        concurrency=max(1, int(_env("REQAUDIT_CONCURRENCY") or 6)),
        vision=_choice("REQAUDIT_VISION", ("auto", "all", "off"),
                       "all" if chosen == "thorough" else "auto"),
        pdf_reader=_choice("REQAUDIT_PDF_READER", ("auto", "pymupdf", "docling"), "auto"),
        markers=_choice("REQAUDIT_MARKERS", ("on", "off"), "on") == "on",
        scan_reads=int(_choice("REQAUDIT_SCAN_READS", ("1", "2"), "2")),
        thinking={role: level for role in ROLES
                  if (level := _choice(f"REQAUDIT_THINKING_{role.upper()}",
                                       ("default", *THINKING_LEVELS), "default")) != "default"},
        warm_up=_choice("REQAUDIT_WARM_UP", ("auto", "on", "off"), "auto"),
        review_passes=int(_choice("REQAUDIT_REVIEW_PASSES", ("1", "2", "3"), "2")),
        notes=notes,
    )
