"""Stage interface and the state shared between stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from reqaudit.config import Settings
from reqaudit.llm import LLMClient
from reqaudit.models import (
    Cell,
    CheckFailure,
    Coverage,
    DocumentIR,
    DocumentProfile,
    FactTable,
    Finding,
    IntakeResult,
    MarkerRecord,
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
    facts: FactTable | None = None
    cells: list[Cell] = Field(default_factory=list)
    plan_notes: list[str] = Field(default_factory=list)
    candidates: list[Finding] = Field(default_factory=list)
    markers: list[MarkerRecord] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    rejected: list[Finding] = Field(default_factory=list)
    check_failures: list[CheckFailure] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    errors: list[StageError] = Field(default_factory=list)


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
