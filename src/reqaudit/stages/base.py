"""Stage interface and the state shared between stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from reqaudit.config import Settings
from reqaudit.llm import LLMClient
from reqaudit.models import (
    Coverage,
    DocumentIR,
    DocumentProfile,
    Finding,
    IntakeResult,
    Question,
    Unit,
)
from reqaudit.trace import Trace


class StageError(BaseModel):
    stage: str
    kind: str
    message: str


class RunState(BaseModel):
    """Everything the stages have produced so far. Serialised to runs/<id>/state.json."""

    intake: IntakeResult | None = None
    document: DocumentIR | None = None
    profile: DocumentProfile | None = None
    units: list[Unit] = Field(default_factory=list)
    coverage: Coverage | None = None
    findings: list[Finding] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    errors: list[StageError] = Field(default_factory=list)


@dataclass
class RunContext:
    input_path: Path
    run_dir: Path
    settings: Settings
    state: RunState
    trace: Trace
    llm: LLMClient


class Stage(ABC):
    code: ClassVar[str]
    title: ClassVar[str]
    artifact: ClassVar[str]

    @abstractmethod
    def run(self, ctx: RunContext) -> BaseModel:
        """Do the work, update `ctx.state`, return the artifact to persist."""
