import json
from pathlib import Path

from reqaudit.cli import main

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def test_run_writes_report_and_artifacts(tmp_path):
    report = tmp_path / "out" / "report.md"
    code = main([str(ORGANIZERS_DOC), str(report), "--runs-dir", str(tmp_path / "runs")])

    assert code == 0
    text = report.read_text(encoding="utf-8")
    for section in ["## Паспорт прогона", "## Как агент нарезал требования",
                    "## Проверка каждого требования", "## Проверка всего набора требований",
                    "## Сводка", "## Итог"]:
        assert section in text
    assert "requirements.md" in text
    # Until checks exist, the report must not suggest the document is fine.
    assert "не означает, что документ без дефектов" in text

    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "s0_intake.json").exists()
    passport = json.loads((run_dir / "passport.json").read_text(encoding="utf-8"))
    assert passport["status"] == "completed"
    assert len(passport["input_sha256"]) == 64
    events = [json.loads(line)["event"]
              for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0] == "run_start" and events[-1] == "run_end"


def test_rejected_input_still_writes_report(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_bytes(b"")
    report = tmp_path / "report.md"

    code = main([str(empty), str(report), "--runs-dir", str(tmp_path / "runs")])

    assert code == 2
    text = report.read_text(encoding="utf-8")
    assert "Документ не принят" in text
    assert "Файл пустой" in text
