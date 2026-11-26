"""The author answers the agent's questions; the agent re-checks the findings they concern."""

import json
from dataclasses import replace
from pathlib import Path

from fakes import fake_team

from reqaudit.answers import fingerprint, parse, render_template
from reqaudit.cli import main
from reqaudit.config import Settings
from reqaudit.models import Question
from reqaudit.pipeline import answers_path_for, run_pipeline

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def q(qid: str, text: str) -> Question:
    return Question(id=qid, finding_ids=["D-001"], addressee="аналитик", text=text, priority=1)


def test_template_round_trip_keeps_answers_and_ignores_stale_ones():
    questions = [q("Q-01", "Какой порог верен?"), q("Q-02", "Кто включает уведомление?")]
    text = render_template(questions, "spec.md")
    assert parse(text) == {}  # empty answers are not answers

    head, questions_part = text.split("## Q-01", 1)
    filled = head + "## Q-01" + questions_part.replace("Ответ:", "Ответ: 50 000 ₸", 1)
    assert parse(filled) == {fingerprint("Какой порог верен?"): "50 000 ₸"}

    # Re-rendering keeps what the author wrote, even with different question numbers.
    again = render_template([q("Q-07", "Какой порог верен?")], "spec.md", parse(filled))
    assert "Ответ: 50 000 ₸" in again

    # An answer whose question changed is not applied to another question.
    assert fingerprint("Какой порог верен?") != fingerprint("Какой лимит верен?")


def test_answers_path_next_to_the_report(tmp_path):
    assert answers_path_for(tmp_path / "r.report.md").name == "r.answers.md"
    assert answers_path_for(tmp_path / "out.md").name == "out.answers.md"


def _settings(tmp_path):
    return replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                   models={"judge": ["fake:team"]})


def _run(tmp_path, model, **kwargs):
    report = tmp_path / "doc.report.md"
    result = run_pipeline(ORGANIZERS_DOC, report, _settings(tmp_path),
                          model_factory=lambda _id: model, **kwargs)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    return result, state, report.read_text(encoding="utf-8")


def test_first_run_leaves_a_questions_file_and_skips_the_recheck(tmp_path):
    model, calls = fake_team()
    _, state, report = _run(tmp_path, model)

    answers_file = tmp_path / "doc.answers.md"
    assert answers_file.exists()
    text = answers_file.read_text(encoding="utf-8")
    assert all(f"## {question['id']} [" in text for question in state["questions"])
    assert calls["reassess"] == 0
    assert "| s8b | Перепроверка по ответам автора | не требовался |" in report
    assert "--answers <файл>" in report


def test_answers_recheck_findings_from_cache_plus_one_call(tmp_path):
    model, calls = fake_team()
    _, first, _ = _run(tmp_path, model)
    review_calls = calls["review"]

    questions = first["questions"]
    answers = {fingerprint(questions[0]["text"]): "Это уже указано в Ф-2.",
               fingerprint(questions[1]["text"]): "Порог задаётся в тенге, суммы переводим."}
    result, second, report = _run(tmp_path, model, answers=answers)

    assert calls["review"] == review_calls  # every earlier stage came from the disk cache
    assert calls["reassess"] == 1
    decisions = {f["id"]: f["answer_decision"] for f in second["findings"]
                 if f["answer_decision"]}
    assert "resolved" in decisions.values() and "confirmed" in decisions.values()
    assert "## Сняты после ответов автора" in report
    assert "**Правка документа по ответу.** Добавить в документ: Порог" in report
    assert "Вопросов с ответом автора | 2 из" in report
    resolved_ids = [i for i, d in decisions.items() if d == "resolved"]
    summary = report.split("## Сводка")[1].split("## Итог")[0]
    assert all(rid not in summary for rid in resolved_ids)  # resolved ones are not counted


def test_ask_in_the_terminal_saves_answers(tmp_path):
    model, calls = fake_team()
    asked = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "Это уже указано в документе." if len(asked) == 1 else ""

    _, state, _ = _run(tmp_path, model, ask=ask)
    assert len(asked) == len(state["questions"])
    assert calls["reassess"] == 1
    saved = parse((tmp_path / "doc.answers.md").read_text(encoding="utf-8"))
    assert list(saved.values()) == ["Это уже указано в документе."]


def test_answers_for_other_questions_are_not_applied(tmp_path):
    model, calls = fake_team()
    _, state, _ = _run(tmp_path, model, answers={fingerprint("Совсем другой вопрос?"): "Да."})
    assert calls["reassess"] == 0
    assert all(f["author_answer"] is None for f in state["findings"])


def test_cli_rejects_missing_answers_file(tmp_path):
    code = main([str(ORGANIZERS_DOC), str(tmp_path / "r.md"), "--answers",
                 str(tmp_path / "nope.md"), "--runs-dir", str(tmp_path / "runs")])
    assert code == 2
