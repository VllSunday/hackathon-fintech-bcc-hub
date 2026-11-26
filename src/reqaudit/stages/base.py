"""Stage interface and the context a stage runs in. The run state itself is a domain
type (models.RunState); it is re-exported here for the stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from reqaudit.config import Settings
from reqaudit.llm import LLMClient
from reqaudit.models import RunState, StageError
from reqaudit.trace import Trace

__all__ = ["RunContext", "RunState", "Stage", "StageError"]


@dataclass
class RunContext:
    input_path: Path
    run_dir: Path
    settings: Settings
    state: RunState
    trace: Trace
    llm: LLMClient
    # The author's answers to the agent's questions: from a file (--answers) or asked in the
    # terminal (--ask). Keyed by question fingerprint, see reqaudit.answers.
    answers: dict[str, str] = field(default_factory=dict)
    ask: Callable[[str], str] | None = None


class Stage(ABC):
    code: ClassVar[str]
    title: ClassVar[str]
    artifact: ClassVar[str]

    @abstractmethod
    def run(self, ctx: RunContext) -> BaseModel:
        """Do the work, update `ctx.state`, return the artifact to persist."""

    def applies(self, ctx: RunContext) -> bool:
        """False when the stage has nothing to do in this run; it is then reported as skipped."""
        return True
