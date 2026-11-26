"""Reads a run folder and shapes it for the interface. Never writes into a run.

Everything shown in the browser comes from the files the pipeline leaves behind:
`trace.jsonl`, the stage artifacts, `state.json`, `passport.json` and `report.md`.
Wording that the Markdown report already has (the verdict, places of quotes) is taken
from the report's own functions, so the two views cannot disagree.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from reqaudit import criteria
from reqaudit.evidence import quote_span
from reqaudit.models import Block, Finding, Severity
from reqaudit.passport import Passport
from reqaudit.pipeline import IMPLEMENTED, STAGE_PLAN
from reqaudit.report.views import (
    display_quote,
    has_checks,
    place,
    rejected_by_gate,
    reported,
    verdict,
)
from reqaudit.stages.base import RunState

RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_ARTIFACTS = {stage.code: stage.artifact for stage in IMPLEMENTED}
# A run without `run_end` whose trace has been silent this long is treated as interrupted.
STALE_AFTER_S = 15 * 60
_PAGE = Path(__file__).with_name("index.html")
_DATA_SLOT = "<!--REPORT_DATA-->"
# Fonts ship with the page (SIL OFL, licences next to them), so it looks the same everywhere.
_FONTS = Path(__file__).with_name("fonts")
_FONT_NAME = re.compile(r"[a-z0-9-]+\.woff2")
_FONT_URL = re.compile(r'url\("fonts/([a-z0-9-]+\.woff2)"\)')


# --- trace and progress -----------------------------------------------------------------


def read_trace(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:  # the last line may be half-written while the run goes on
            continue
    return events


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return round(delta.total_seconds(), 3)


def progress(run_dir: Path, alive: bool | None = None) -> dict[str, Any]:
    """Stage chain of a run from its trace: status, time and model calls per stage.

    `alive` is what the server knows about the process; None means the run was started
    elsewhere and only the trace can tell.
    """
    events = read_trace(run_dir)
    stages: dict[str, dict[str, Any]] = {
        code: {"code": code, "title": title, "status": "pending", "seconds": None,
               "llm_calls": 0, "cache_hits": 0, "llm_failed": 0, "started_at": None,
               "error": None,
               "summary": {}, "artifact": _ARTIFACTS.get(code)}
        for code, title in STAGE_PLAN
    }
    out: dict[str, Any] = {"run_id": run_dir.name, "started_at": None, "input": None,
                           "profile": None, "finished": False, "status": None,
                           "exit_code": None}
    current: str | None = None
    previous_ts: str | None = None
    for e in events:
        kind, ts = e.get("event"), e.get("ts")
        stage = stages.get(e.get("stage") or "")
        if kind == "run_start":
            out.update(started_at=ts, input=Path(str(e.get("input", ""))).name,
                       profile=e.get("profile"))
        elif kind == "stage_start" and stage:
            stage.update(status="running", started_at=ts)
            current = stage["code"]
        elif kind == "stage_skipped" and stage:
            stage["status"] = "not_needed"  # the stage had nothing to do in this run
        elif kind == "stage_end" and stage:
            stage.update(status="done", seconds=e.get("seconds"))
            current = None
        elif kind == "stage_error" and stage:
            stage.update(status="error", error=e.get("message"),
                         seconds=_seconds_between(stage["started_at"], ts))
            current = None
        elif kind == "llm_call" and current:
            if e.get("cache_hit"):
                stages[current]["cache_hits"] += 1
            else:
                stages[current]["llm_calls"] += int(e.get("requests") or 1)
        elif kind == "llm_fallback" and current:
            stages[current]["llm_failed"] += 1  # the model gave up on this request
        elif isinstance(kind, str) and kind.endswith("_summary") and stage:
            stage["summary"] = {k: v for k, v in e.items()
                                if k not in ("ts", "event", "stage")}
        elif kind == "run_end":
            # s9 is the report itself: it has no stage events, only the end of the run.
            stages["s9"].update(status="done", seconds=_seconds_between(previous_ts, ts))
            out.update(finished=True, status=e.get("status"), exit_code=e.get("exit_code"))
        if kind != "run_end":
            previous_ts = ts

    for stage in stages.values():
        artifact = stage["artifact"]
        stage["artifact_exists"] = bool(artifact and (run_dir / artifact).exists())

    if out["finished"]:
        for stage in stages.values():
            if stage["status"] == "pending":
                stage["status"] = "skipped"
    else:
        trace = run_dir / "trace.jsonl"
        silent = time.time() - trace.stat().st_mtime if trace.exists() else STALE_AFTER_S
        if alive is False or (alive is None and silent >= STALE_AFTER_S):
            out["status"] = "interrupted"
            for stage in stages.values():
                if stage["status"] == "running":
                    stage["status"] = "interrupted"
        else:
            out["status"] = "running"
    out["stages"] = list(stages.values())
    out["has_report"] = (run_dir / "state.json").exists()
    return out


# --- list of runs -----------------------------------------------------------------------


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_summary(run_dir: Path, alive: bool | None = None) -> dict[str, Any]:
    """One row of the runs list. Counts appear only when the run has a state file."""
    passport = _load_json(run_dir / "passport.json")
    row: dict[str, Any] = {"run_id": run_dir.name, "has_report": False}
    if passport:
        row.update(document=passport.get("document_name"), title=passport.get("document_title"),
                   started_at=passport.get("started_at"), status=passport.get("status"),
                   profile=passport.get("profile"), models=passport.get("models_used") or [])
    else:
        live = progress(run_dir, alive)
        row.update(document=live["input"], started_at=live["started_at"],
                   status=live["status"], profile=live["profile"], models=[])
    state = load_state(run_dir)
    if state:
        counted = reported(state)
        row.update(has_report=True, units=len(state.units), findings=len(counted),
                   critical=sum(1 for f in counted if f.severity == Severity.CRITICAL),
                   questions=len(state.questions), checks_ran=has_checks(state))
    return row


def load_state(run_dir: Path) -> RunState | None:
    """The run's state.json, or None while the run has not written a readable one."""
    try:
        return RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_runs(runs_dir: Path, alive: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    if not runs_dir.is_dir():
        return []
    alive = alive or {}
    dirs = [d for d in runs_dir.iterdir() if d.is_dir() and RUN_ID.fullmatch(d.name)]
    return [run_summary(d, alive.get(d.name)) for d in sorted(dirs, key=lambda d: d.name,
                                                             reverse=True)]


# --- where a quote sits inside its block ------------------------------------------------


# --- report bundle ----------------------------------------------------------------------


def _verdict_data(state: RunState, confirmed: list[Finding]) -> dict[str, Any]:
    """The verdict as data: the same values the Markdown report prints."""
    v = verdict(state, confirmed)
    return {"word": v.word, "text": v.text, "level": v.level, "incomplete": v.incomplete,
            "first": [f.id for f in v.first]}


def _finding_view(f: Finding, blocks: dict[str, Block]) -> dict[str, Any]:
    """A finding as the page shows it: each quote with its place and its span in the block."""
    item = f.model_dump(mode="json")
    for evidence, raw in zip(item["evidence"], f.evidence, strict=True):
        block = blocks.get(raw.block_id)
        span = quote_span(raw.quote, block.text) if block else None
        evidence["place"] = place([raw.block_id], blocks)
        evidence["display"] = display_quote(raw.quote)
        evidence["span"] = list(span) if span else None
    return item


def _editor_counts(run_dir: Path) -> dict[str, int] | None:
    """How many findings the editor (s8) received and kept after merging duplicates."""
    for event in reversed(read_trace(run_dir)):
        if event.get("event") == "editor_summary":
            return {"before": event.get("findings_before"), "after": event.get("findings_after")}
    return None


def build_report(run_dir: Path) -> dict[str, Any]:
    """Everything the report screen shows, from the files of one finished run.

    Raises FileNotFoundError when the run has not written its state yet.
    """
    state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
    passport = Passport.model_validate_json(
        (run_dir / "passport.json").read_text(encoding="utf-8"))
    blocks: dict[str, Block] = ({b.id: b for b in state.document.blocks}
                                if state.document else {})
    confirmed = reported(state)
    checks_ran = has_checks(state)
    findings = [_finding_view(f, blocks) for f in confirmed]
    resolved = [_finding_view(f, blocks) for f in state.findings if f.resolved]

    units = []
    for u in state.units:
        item = u.model_dump(mode="json")
        item["place"] = place(u.block_ids, blocks)
        units.append(item)

    by_gate = sum(1 for f in state.rejected if rejected_by_gate(f))
    intake = state.intake.model_dump(mode="json", exclude={"text", "path"}) \
        if state.intake else None
    document = state.document.model_dump(mode="json") if state.document else None
    facts = state.facts.model_dump(mode="json") if state.facts else None
    for fact in (facts or {}).get("facts", []):
        fact["display"] = display_quote(fact["quote"])
    gate = _load_json(run_dir / "s7_gated.json")
    report_md = run_dir / "report.md"

    return {
        "run_id": run_dir.name,
        "passport": passport.model_dump(mode="json"),
        "intake": intake,
        "document": document,
        "profile": state.profile.model_dump(mode="json") if state.profile else None,
        "coverage": state.coverage.model_dump(mode="json") if state.coverage else None,
        "units": units,
        "facts": facts,
        "cells": [c.model_dump(mode="json") for c in state.cells],
        "findings": findings,
        "resolved": resolved,
        "markers": [m.model_dump(mode="json") for m in state.markers],
        "plan_notes": state.plan_notes,
        "rejected_findings": [{"id": f.id, "title": f.title, "criterion": f.criterion.value,
                               "severity": f.severity.value, "review_note": f.review_note,
                               "by_gate": rejected_by_gate(f)}
                              for f in state.rejected],
        "editor": _editor_counts(run_dir),
        "questions": [q.model_dump(mode="json") for q in state.questions],
        "check_failures": [c.model_dump(mode="json") for c in state.check_failures],
        "errors": [e.model_dump(mode="json") for e in state.errors],
        "completed": state.completed,
        "checks_ran": checks_ran,
        "rejected": {"by_review": len(state.rejected) - by_gate, "by_gate": by_gate},
        "gate_stats": gate.get("stats") if isinstance(gate, dict) else None,
        "verdict": _verdict_data(state, confirmed) if checks_ran else None,
        "criteria": {info.code.value: {"label": info.label_ru, "level": info.level.value,
                                       "standard": info.standard_ru}
                     for info in criteria.CRITERIA.values()},
        "progress": progress(run_dir),
        "report_md": report_md.read_text(encoding="utf-8") if report_md.exists() else None,
    }


# --- pages ------------------------------------------------------------------------------


def page() -> str:
    return _PAGE.read_text(encoding="utf-8")


def font_path(name: str) -> Path | None:
    """A font file shipped with the page, or None for any other name."""
    if not _FONT_NAME.fullmatch(name):
        return None
    path = _FONTS / name
    return path if path.is_file() else None


def _inline_font(match: re.Match[str]) -> str:
    path = font_path(match.group(1))
    if path is None:
        return 'url("")'  # a missing file falls back to the system font, never to the network
    return f'url("data:font/woff2;base64,{base64.b64encode(path.read_bytes()).decode()}")'


def render_html(bundle: dict[str, Any]) -> str:
    """The report screen as one file: the interface page with the run's data and fonts inside."""
    # "<" never appears raw, so the data cannot close its own script tag.
    data = json.dumps(bundle, ensure_ascii=False).replace("<", "\\u003c")
    tag = f'<script id="report-data" type="application/json">{data}</script>'
    return _FONT_URL.sub(_inline_font, page()).replace(_DATA_SLOT, tag, 1)


def write_html(run_dir: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(build_report(run_dir)), encoding="utf-8")
    return target
