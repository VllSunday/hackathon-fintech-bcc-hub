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


# --- gold files: traps, checking, importing generator output --------------------------

from reqaudit.eval import (  # noqa: E402
    GoldFile,
    Trap,
    check_gold,
    import_generated,
    load_gold_file,
)

TEXT = "\n".join(b["text"] for b in BLOCKS)


def test_check_gold_finds_quotes_that_would_make_scoring_lie():
    gold = GoldFile(document="x.md", defects=[
        GoldDefect(id="G-01", scope="pair", criterion="set_consistent", severity="critical",
                   quotes=["Сервис передаёт push на отправку в течение минуты"]),
        GoldDefect(id="G-02", scope="requirement", criterion="clarity", severity="huge",
                   quotes=["Этого текста нет в документе"]),
        GoldDefect(id="G-03", scope="requirement", criterion="verifiable", severity="major",
                   quotes=["push"]),
    ], traps=[Trap(id="T-01", quotes=["Когда расходы переходят порог, клиент получает push."])])
    problems = "\n".join(check_gold(gold, TEXT))
    assert "G-01: у пары одна цитата" in problems
    assert "G-02: цитаты нет в документе" in problems
    assert "неизвестный критерий «clarity»" in problems and "неизвестная серьёзность" in problems
    assert "G-03: цитата слишком короткая" in problems
    assert "T-01" not in problems


def test_traps_count_findings_that_match_no_gold_defect(tmp_path):
    trap = Trap(id="T-01", quotes=["Когда расходы переходят порог, клиент получает push."])
    on_trap = finding("D-009", "unambiguous", "minor",
                      "Когда расходы переходят порог, клиент получает push.")
    vague = finding("D-003", "verifiable", "major", "в течение минуты после получения события")
    score = score_run(GOLD, run_dir(tmp_path, "r1", [vague, on_trap]), [trap])
    assert score.traps_hit == {"T-01": "D-009"} and score.traps_total == 1
    assert "| 1/1 |" in report(GOLD, [score])


def test_generator_answer_is_split_into_document_and_gold(tmp_path):
    raw = """Вот документ.
=== DOCUMENT BEGIN ===
# Часть 1
Ф-1. Перевод до 500 000 ₸ за операцию.
=== DOCUMENT END ===
дальше
=== DOCUMENT BEGIN ===
Ф-2. Лимит в API: amount | integer | 100–1 000 000
=== DOCUMENT END ===
=== GOLD BEGIN ===
```yaml
document: near/demo.md
annotators: [generator]
requirements_total: 2
defects:
  - id: G-01
    scope: pair
    criterion: set_consistent
    severity: critical
    kind: cross_section
    quotes:
      - "Перевод до 500 000 ₸ за операцию."
      - "amount | integer | 100–1 000 000"
traps: []
```
=== GOLD END ==="""
    doc, gold_path = import_generated(raw, tmp_path)
    assert doc == tmp_path / "inputs" / "near" / "demo.md"
    assert "Ф-1." in doc.read_text(encoding="utf-8") and "Ф-2." in doc.read_text(encoding="utf-8")
    gold = load_gold_file(gold_path)
    assert gold.requirements_total == 2 and gold.defects[0].kind == "cross_section"
    assert check_gold(gold, doc.read_text(encoding="utf-8")) == []
