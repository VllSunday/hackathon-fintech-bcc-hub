"""Scoring saved runs against a gold annotation: by place, one to one, no model calls."""

import json
from pathlib import Path

from reqaudit.eval import GoldDefect, report, score_run

GOLD = [
    GoldDefect(id="G-01", scope="pair", criterion="set_consistent", severity="critical",
               quotes=["На карту приходится максимум один такой push за календарный месяц.",
                       "следующее достижение вызывает push независимо от истории"]),
    GoldDefect(id="G-02", scope="requirement", criterion="verifiable", severity="major",
               quotes=["Сервис передаёт push на отправку в течение минуты"]),
]


BLOCKS = [
    {"id": "b1", "text": "* **Ф-3.** Когда расходы переходят порог, клиент получает push. "
                         "На карту приходится максимум один такой push за календарный месяц."},
    {"id": "b2", "text": "* **Ф-4.** При изменении порога накопленная сумма сохраняется. Если она "
                         "ниже нового порога, следующее достижение вызывает push независимо "
                         "от истории предыдущей настройки."},
    {"id": "b3", "text": "Сервис передаёт push на отправку в течение минуты после получения "
                         "события."},
]


def _block_of(quote: str) -> str:
    from reqaudit.evidence import match_key
    return next((b["id"] for b in BLOCKS if match_key(quote) in match_key(b["text"])), "b9")


def finding(fid, criterion, severity, *quotes, decision=None):
    return {"id": fid, "criterion": criterion, "severity": severity, "title": fid,
            "answer_decision": decision,
            "evidence": [{"block_id": _block_of(q), "quote": q} for q in quotes]}


def run_dir(tmp_path: Path, name: str, findings: list[dict]) -> Path:
    path = tmp_path / name
    path.mkdir()
    state = {"findings": findings, "document": {"blocks": BLOCKS}}
    (path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return path


def test_conflict_needs_both_sides_and_matching_is_one_to_one(tmp_path):
    one_side = finding("D-001", "set_consistent", "critical",
                       "На карту приходится максимум один такой push за календарный месяц.")
    both = finding("D-002", "set_consistent", "critical",
                   "Ф-3. На карту приходится максимум один такой push за календарный месяц.",
                   "Если она ниже нового порога, следующее достижение вызывает push "
                   "независимо от истории предыдущей настройки.")
    vague = finding("D-003", "verifiable", "minor", "в течение минуты после получения события")
    score = score_run(GOLD, run_dir(tmp_path, "r1", [one_side, both, vague]))

    assert score.matched == {"G-01": "D-002", "G-02": "D-003"}
    assert score.recall == 1.0 and round(score.precision, 2) == 0.67
    assert score.severity_agrees == 1  # G-02 is major, the run said minor
    assert [f[0] for f in score.unmatched_findings] == ["D-001"]


def test_resolved_findings_do_not_count(tmp_path):
    resolved = finding("D-001", "verifiable", "major",
                       "Сервис передаёт push на отправку в течение минуты", decision="resolved")
    assert score_run(GOLD, run_dir(tmp_path, "r1", [resolved])).matched == {}


def test_stability_across_runs(tmp_path):
    first = run_dir(tmp_path, "r1", [finding(
        "D-001", "verifiable", "major", "Сервис передаёт push на отправку в течение минуты")])
    second = run_dir(tmp_path, "r2", [finding(
        "D-004", "verifiable", "minor", "Сервис передаёт push на отправку в течение минуты")])
    text = report(GOLD, [score_run(GOLD, first), score_run(GOLD, second)])
    assert "найдены во всех прогонах — 1, только в части — 0" in text
    assert "серьёзность менялась между прогонами у 1" in text
    assert "| G-01 | critical | 0/2 |" in text
