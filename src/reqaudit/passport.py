"""Run passport: what was checked, when, by which agent version and models."""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime
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
    models: dict[str, str] = Field(default_factory=dict)
    llm_calls: int = 0
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


def git_state() -> tuple[str | None, bool | None]:
    """Commit and dirty flag of the agent's own checkout.

    Falls back to REQAUDIT_GIT_COMMIT, which a Docker build can bake in.
    """
    commit = _git("rev-parse", "HEAD")
    if commit:
        status = _git("status", "--porcelain", "--untracked-files=no")
        return commit, bool(status)
    return os.environ.get("REQAUDIT_GIT_COMMIT") or None, None


def start(document_name: str, profile: str, models: dict[str, str], run_id: str,
          run_dir: Path) -> Passport:
    commit, dirty = git_state()
    return Passport(
        document_name=document_name,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        git_commit=commit,
        git_dirty=dirty,
        profile=profile,
        models=dict(models),
        prompts_fingerprint=prompt_store.fingerprint(),
        run_id=run_id,
        run_dir=run_dir.as_posix(),
    )
