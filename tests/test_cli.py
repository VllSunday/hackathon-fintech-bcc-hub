import json
from pathlib import Path

from reqaudit.cli import main

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def test_run_without_model_reads_document_and_explains(tmp_path):
    report = tmp_path / "out" / "report.md"
    code = main([str(ORGANIZERS_DOC), str(report), "--runs-dir", str(tmp_path / "runs")])

    # No model is configured in tests: reading works, segmentation stops with a clear reason.
    assert code == 3
    text = report.read_text(encoding="utf-8")
    for section in ["## Паспорт прогона", "## Как агент нарезал требования",
                    "## Проверка каждого требования", "## Проверка всего набора требований",
                    "## Сводка", "## Итог"]:
        assert section in text
    assert "requirements.md" in text
    assert "Блоков текста | 124" in text
    assert "Нет модели для проверки: задайте OPENAI_API_KEY или GEMINI_API_KEY" in text
    # Until checks exist, the report must not suggest the document is fine.
    assert "не означает, что документ без дефектов" in text

    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "s0_intake.json").exists()
    assert (run_dir / "s1_blocks.json").exists()
    passport = json.loads((run_dir / "passport.json").read_text(encoding="utf-8"))
    assert passport["status"] == "partial"
    assert len(passport["input_sha256"]) == 64
    events = [json.loads(line)["event"]
              for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0] == "run_start" and events[-1] == "run_end"


def test_broken_pdf_is_rejected_with_a_reason(tmp_path):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")  # a header and nothing else
    report = tmp_path / "report.md"

    code = main([str(pdf), str(report), "--runs-dir", str(tmp_path / "runs")])

    assert code == 2
    text = report.read_text(encoding="utf-8")
    assert "Документ не принят" in text and "PDF" in text
    assert "| s0 | Приёмка | выполнен |" in text


def test_report_path_defaults_to_reports_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    doc = tmp_path / "spec.md"
    doc.write_text("# ТЗ\n\n* Клиент задаёт порог расходов.\n", encoding="utf-8")

    main([str(doc), "--runs-dir", str(tmp_path / "runs")])

    assert (tmp_path / "reports" / "spec.report.md").exists()


def test_rejected_input_still_writes_report(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_bytes(b"")
    report = tmp_path / "report.md"

    code = main([str(empty), str(report), "--runs-dir", str(tmp_path / "runs")])

    assert code == 2
    text = report.read_text(encoding="utf-8")
    assert "Документ не принят" in text
    assert "Файл пустой" in text
