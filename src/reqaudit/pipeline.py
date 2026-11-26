"""Runs the stages in order, persists each artifact, and always writes a report."""

from __future__ import annotations

import secrets
import shutil
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reqaudit import passport as passport_mod
from reqaudit.answers import parse as parse_answers
from reqaudit.answers import render_template
from reqaudit.config import Settings
from reqaudit.errors import InputError
from reqaudit.llm import LLMClient, LLMError
from reqaudit.llm.client import ModelFactory
from reqaudit.report.render import render_markdown, spend_line
from reqaudit.stages.base import RunContext, RunState, Stage, StageError
from reqaudit.stages.s0_intake import IntakeStage
from reqaudit.stages.s1_read import ReadStage
from reqaudit.stages.s2_segment import SegmentStage
from reqaudit.stages.s3_facts import FactsStage
from reqaudit.stages.s4_plan import PlanStage
from reqaudit.stages.s5_candidates import CandidatesStage
from reqaudit.stages.s6_review import ReviewStage
from reqaudit.stages.s7_gates import GatesStage
from reqaudit.stages.s8_questions import QuestionsStage
from reqaudit.stages.s8b_answers import AnswersStage
from reqaudit.trace import Trace

# The full plan. Stages without an implementation are listed so the report can say so.
STAGE_PLAN: list[tuple[str, str]] = [
    ("s0", "Приёмка"),
    ("s1", "Чтение"),
    ("s2", "Профиль и нарезка"),
    ("s3", "Факты"),
    ("s4", "План ячеек"),
    ("s5", "Кандидаты в дефекты"),
    ("s6", "Оппонент и арбитр"),
    ("s7", "Гейты доказательств"),
    ("s8", "Сведение и вопросы"),
    ("s8b", "Перепроверка по ответам автора"),
    ("s9", "Отчёт"),
]

IMPLEMENTED: list[Stage] = [IntakeStage(), ReadStage(), SegmentStage(), FactsStage(),
                            PlanStage(), CandidatesStage(), ReviewStage(), GatesStage(),
                            QuestionsStage(), AnswersStage()]

EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_PARTIAL = 3


@dataclass
class RunResult:
    exit_code: int
    report_path: Path
    run_dir: Path
    status: str
    spend: str = ""


def _write_answers_file(path: Path, state: RunState, document_name: str,
                        answers: dict[str, str]) -> None:
    """Questions with room for answers. Answers already in the file are never lost."""
    existing = parse_answers(path.read_text(encoding="utf-8")) if path.exists() else {}
    path.write_text(render_template(state.questions, document_name, {**existing, **answers}),
                    encoding="utf-8")


def _new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def answers_path_for(output_path: Path) -> Path:
    """`reports/x.report.md` -> `reports/x.answers.md`; otherwise `<name>.answers.md`."""
    name = output_path.name
    if name.endswith(".report.md"):
        return output_path.with_name(name[: -len(".report.md")] + ".answers.md")
    return output_path.with_name(output_path.stem + ".answers.md")


def run_pipeline(input_path: Path, output_path: Path, settings: Settings,
                 model_factory: ModelFactory | None = None,
                 answers: dict[str, str] | None = None,
                 ask: Callable[[str], str] | None = None) -> RunResult:
    """Run all implemented stages and write the report.

    `answers` are the author's answers from an answers file, `ask` asks them in the terminal;
    either enables the re-check stage s8b. `model_factory` replaces real models in tests.
    """
    run_id = _new_run_id()
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = Trace(run_dir / "trace.jsonl")
    state = RunState()
    llm = LLMClient(settings, trace, model_factory=model_factory)
    ctx = RunContext(input_path=input_path, run_dir=run_dir, settings=settings,
                     state=state, trace=trace, llm=llm, answers=dict(answers or {}), ask=ask)
    passport = passport_mod.start(input_path.name, settings.profile, settings.models, run_id,
                                  run_dir)
    passport.setup_notes = list(settings.notes)

    trace.event("run_start", run_id=run_id, input=str(input_path), profile=settings.profile)
    status = "completed"
    exit_code = EXIT_OK

    for stage in IMPLEMENTED:
        if status == "completed" and not stage.applies(ctx):
            state.skipped.append(stage.code)
            trace.event("stage_skipped", stage=stage.code)
            continue
        trace.event("stage_start", stage=stage.code)
        started = time.perf_counter()
        try:
            artifact = stage.run(ctx)
        except InputError as err:
            state.errors.append(StageError(stage=stage.code, kind=err.code, message=err.message))
            trace.event("stage_error", stage=stage.code, kind=err.code, message=err.message)
            status, exit_code = "input_rejected", EXIT_INPUT_ERROR
            break
        except LLMError as err:
            state.errors.append(StageError(stage=stage.code, kind=type(err).__name__,
                                           message=err.message))
            trace.event("stage_error", stage=stage.code, kind=type(err).__name__,
                        message=err.message)
            status, exit_code = "partial", EXIT_PARTIAL
            break
        except Exception as err:  # noqa: BLE001 - the report must be written regardless
            state.errors.append(StageError(stage=stage.code, kind=type(err).__name__,
                                           message=str(err)))
            trace.event("stage_error", stage=stage.code, kind=type(err).__name__,
                        message=str(err), traceback=traceback.format_exc())
            status, exit_code = "partial", EXIT_PARTIAL
            break
        (run_dir / stage.artifact).write_text(
            artifact.model_dump_json(indent=2), encoding="utf-8")
        state.completed.append(stage.code)
        trace.event("stage_end", stage=stage.code,
                    seconds=round(time.perf_counter() - started, 3))

    if state.intake:
        passport.input_sha256 = state.intake.sha256
    if state.document and state.document.title:
        passport.document_title = state.document.title
    if status == "completed" and state.check_failures:
        # All stages ran, but some checks inside them did not: the report is incomplete.
        status, exit_code = "checks_incomplete", EXIT_PARTIAL
    passport.status = status
    passport.llm_calls = llm.requests
    passport.llm_cache_hits = llm.cache_hits
    passport.llm_input_tokens = llm.input_tokens
    passport.llm_cached_tokens = llm.cached_tokens
    passport.llm_output_tokens = llm.output_tokens
    passport.llm_cost_usd = round(llm.cost_usd, 4)
    passport.llm_unpriced_calls = llm.unpriced_calls
    passport.models_used = list(llm.models_used)
    passport.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")

    # s9 is the report itself; it runs on every outcome.
    state.completed.append("s9")
    report = render_markdown(passport, state, STAGE_PLAN)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    shutil.copyfile(output_path, run_dir / "report.md")
    (run_dir / "passport.json").write_text(passport.model_dump_json(indent=2), encoding="utf-8")
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    if state.questions:
        _write_answers_file(answers_path_for(output_path), state, input_path.name, ctx.answers)
    trace.event("run_end", status=status, exit_code=exit_code)

    return RunResult(exit_code=exit_code, report_path=output_path, run_dir=run_dir, status=status,
                     spend=spend_line(passport))
