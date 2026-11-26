"""Marker words: code finds candidates, the reviewer decides on each one in context.

Cells without markers must send exactly the request they sent before markers existed,
so the dictionary can only add work where it found something.
"""

import json
from dataclasses import replace
from pathlib import Path

from fakes import fake_team, honest_markers, honest_review

from reqaudit.config import Settings
from reqaudit.markers import find_markers
from reqaudit.models import Unit, UnitKind
from reqaudit.pipeline import run_pipeline

PLAIN_DOC = """# Требования к сервису уведомлений

## Функциональные требования

- Сервис хранит историю уведомлений 90 дней.
- Push отправляется клиенту не позднее 60 секунд после покупки.
- Порог расходов задаётся в тенге целым числом от 1 000 до 10 000 000.
"""

MARKED_DOC = """# Требования к сервису уведомлений

## Функциональные требования

- Система должна быстро отправлять push клиенту после покупки.
- Интерфейс настройки порога должен быть удобным и понятным.
- Клиент может выбрать канал: push, SMS и т.д.
- Сервис хранит историю уведомлений 90 дней.
"""


def unit(uid: str, quote: str) -> Unit:
    return Unit(id=uid, kind=UnitKind.FUNCTIONAL, block_ids=["b0001"], quote=quote,
                summary="—")


# --- the dictionary ---------------------------------------------------------------------


def test_finds_marker_words_by_stem_and_phrase():
    hits = find_markers([unit("R-001", "Интерфейс должен быть Удобным, при необходимости "
                                       "поддерживать экспорт в PDF, Excel и т.д.; TBD")])
    words = {h.word.lower(): h.category for h in hits}
    assert words["удобным"] == "subjective"
    assert words["при необходимости"] == "loophole"
    assert words["и т.д."] == "open_list"
    assert words["tbd"] == "unfinished"
    assert words["поддерживать"] == "generic"
    assert [h.id for h in hits] == [f"M-{n:03d}" for n in range(1, len(hits) + 1)]


def test_markers_need_whole_words_and_repeat_once_per_requirement():
    hits = find_markers([
        unit("R-001", "Обработка выполняется быстро, очень быстро."),
        unit("R-002", "Сумма покупки хранится в тиынах; около — не слово, а «околоток»."),
        unit("R-003", "Ответ приходит за 200 мс на 95-м перцентиле."),
    ])
    assert [(h.unit_id, h.word) for h in hits] == [("R-001", "быстро"), ("R-002", "около")]


# --- in the pipeline --------------------------------------------------------------------


def run(tmp_path: Path, document: Path, factory, **overrides):
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]}, **overrides)
    result = run_pipeline(document, tmp_path / "report.md", settings, model_factory=factory)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    return result, state, report, trace


def marked_doc(tmp_path: Path) -> Path:
    path = tmp_path / "marked.md"
    path.write_text(MARKED_DOC, encoding="utf-8")
    return path


def test_without_markers_the_review_requests_are_byte_identical(tmp_path):
    """A document without marker words: a run with markers on must be served entirely
    from the cache of a run with markers off."""
    document = tmp_path / "plain.md"
    document.write_text(PLAIN_DOC, encoding="utf-8")
    assert not find_markers([unit("R-001", PLAIN_DOC)])
    model, calls = fake_team()
    run(tmp_path, document, lambda _id: model, markers=False)
    before = dict(calls)
    result, state, report, _ = run(tmp_path, document, lambda _id: model)
    assert result.exit_code == 0
    assert calls == before  # no new model call: every request hit the cache
    assert state["markers"] == [] and calls["markers"] == 0
    assert "Слов-маркеров" not in report


def test_reviewer_decides_on_every_marker_and_report_counts_them(tmp_path):
    model, calls = fake_team()
    result, state, report, trace = run(tmp_path, marked_doc(tmp_path), lambda _id: model)

    assert result.exit_code == 0, report
    assert calls["markers"] >= 1
    words = {m["word"].lower() for m in state["markers"]}
    assert {"быстро", "удобным", "понятным", "и т.д."} <= words
    assert all(m["defect"] is not None and m["reason"] for m in state["markers"])
    judged = sum(1 for m in state["markers"] if m["defect"])
    assert f"| {len(state['markers'])} / {judged} |" in report
    assert "s5_markers" in trace  # the prompt fingerprint is recorded


def test_undecided_marker_goes_back_to_the_reviewer(tmp_path):
    def skips_one(prompt):
        answer = honest_review(prompt)
        decisions = honest_markers(prompt, answer["findings"])
        return {**answer, "markers": decisions[1:]}

    model, _ = fake_team(review=skips_one)
    _, _, _, trace = run(tmp_path, marked_doc(tmp_path), lambda _id: model)
    assert "`markers` must decide on every marker" in trace


def test_marker_judged_a_defect_needs_a_finding(tmp_path):
    def says_defect_but_reports_nothing(prompt):
        answer = honest_review(prompt)
        decisions = [dict(d, defect=True) for d in honest_markers(prompt, [])]
        return {**answer, "findings": [], "markers": decisions}

    model, _ = fake_team(review=says_defect_but_reports_nothing)
    _, _, _, trace = run(tmp_path, marked_doc(tmp_path), lambda _id: model)
    assert "was judged a defect, but no finding names that requirement" in trace


def test_markers_can_be_switched_off(tmp_path):
    model, calls = fake_team()
    _, state, _, trace = run(tmp_path, marked_doc(tmp_path), lambda _id: model, markers=False)
    assert calls["markers"] == 0 and state["markers"] == []
    assert "s5_markers" not in trace
