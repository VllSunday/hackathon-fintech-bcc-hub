"""Run passport: what was checked, when, by which agent version and models."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, Field

from reqaudit import __version__, prompt_store

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Passport(BaseModel):
    document_name: str
    document_title: str | None = None
    input_sha256: str | None = None
    started_at: str
    finished_at: str | None = None
    agent_version: str = __version__
    git_commit: str | None = None
    git_dirty: bool | None = None
    profile: str
    models: dict[str, list[str]] = Field(default_factory=dict)
    models_used: list[str] = Field(default_factory=list)
    llm_calls: int = 0
    llm_cache_hits: int = 0
    llm_input_tokens: int = 0
    llm_cached_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0
    llm_unpriced_calls: int = 0
    setup_notes: list[str] = Field(default_factory=list)
    prompts_fingerprint: str
    python: str = platform.python_version()
    run_id: str
    run_dir: str
    status: str = "running"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def installed_commit() -> str | None:
    """The commit pip or uv installed the agent from (`uvx --from git+…`), per PEP 610."""
    try:
        text = metadata.distribution("reqaudit").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    try:
        info = json.loads(text or "{}").get("vcs_info") or {}
    except json.JSONDecodeError:
        return None
    return info.get("commit_id") or None


def git_state() -> tuple[str | None, bool | None]:
    """Commit and dirty flag of the agent's own checkout.

    An agent installed from the repository (`uvx --from git+…`) has no checkout: its commit
    is read from the installation record. REQAUDIT_GIT_COMMIT, which a Docker build can bake
    in, is the last resort.
    """
    commit = _git("rev-parse", "HEAD")
    if commit:
        status = _git("status", "--porcelain", "--untracked-files=no")
        return commit, bool(status)
    return installed_commit() or os.environ.get("REQAUDIT_GIT_COMMIT") or None, None


def start(document_name: str, profile: str, models: dict[str, list[str]], run_id: str,
          run_dir: Path) -> Passport:
    commit, dirty = git_state()
    return Passport(
        document_name=document_name,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        git_commit=commit,
        git_dirty=dirty,
        profile=profile,
        models={role: list(chain) for role, chain in models.items()},
        prompts_fingerprint=prompt_store.fingerprint(),
        run_id=run_id,
        run_dir=run_dir.as_posix(),
    )
