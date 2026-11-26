import json
import re
from dataclasses import replace
from pathlib import Path

from fakes import fake_team
from fastapi.testclient import TestClient

from reqaudit.cli import main
from reqaudit.config import Settings
from reqaudit.evidence import match_key
from reqaudit.pipeline import STAGE_PLAN, run_pipeline
from reqaudit.ui import rundata
from reqaudit.ui.server import create_app

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def full_run(tmp_path: Path) -> Path:
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    model, _ = fake_team()
    return run_pipeline(ORGANIZERS_DOC, tmp_path / "report.md", settings,
                        model_factory=lambda _id: model).run_dir


# --- where a quote sits in its block ----------------------------------------------------


def test_quote_span_ignores_markup_case_and_dashes():
    block = "* **Ф-1.** Держатель карты задаёт порог — от 1 000 до 1 000 000 ₸."
    span = rundata.quote_span("ф-1. держатель карты задаёт порог - от 1 000", block)

    assert span is not None
    assert block[span[0]:span[1]] == "Ф-1.** Держатель карты задаёт порог — от 1 000"


def test_quote_span_of_a_table_row_covers_the_cells():
    block = "| Расходы 48 500 ₸ | Провести покупку | Итог 50 000 ₸ |"
    span = rundata.quote_span("Провести покупку | Итог 50 000 ₸", block)

    assert block[span[0]:span[1]] == "Провести покупку | Итог 50 000 ₸"


def test_quote_span_is_none_for_text_that_is_not_there():
    block = "Порог от 1 000 до 1 000 000 ₸."

    assert rundata.quote_span("порог от 2 000 до 1 000 000", block) is None
    assert rundata.quote_span("порог", block) is None  # too short to locate


# --- report data from a finished run ----------------------------------------------------


def test_report_numbers_and_verdict_match_the_run_files(tmp_path):
    run_dir = full_run(tmp_path)
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")

    report = rundata.build_report(run_dir)

    assert len(report["units"]) == len(state["units"]) > 0
    assert len(report["findings"]) == len(state["findings"]) > 0
    assert len(report["questions"]) == len(state["questions"])
    assert f"| Нарушений найдено | {len(report['findings'])} |" in markdown
    # The verdict is the Markdown report's own sentence, not a second opinion.
    verdict = report["verdict"]
    assert f"**{verdict['word']}.** {verdict['text']}" in markdown
    assert all(f"{i} — " in markdown for i in verdict["first"])
    assert report["passport"]["prompts_fingerprint"]
    assert "text" not in report["intake"]  # the document is shown from blocks, once


def test_findings_resolved_by_the_author_leave_the_ui_as_they_leave_the_report(tmp_path):
    """The UI and report.md count findings by one rule (Finding.in_report)."""
    from reqaudit.passport import Passport
    from reqaudit.pipeline import STAGE_PLAN
    from reqaudit.report.render import render_markdown
    from reqaudit.stages.base import RunState

    run_dir = full_run(tmp_path)
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    for f in state["findings"]:
        if f["severity"] == "critical":
            f.update(answer_decision="resolved", author_answer="Это описано в разделе 3.")
    (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    markdown = render_markdown(
        Passport.model_validate_json((run_dir / "passport.json").read_text(encoding="utf-8")),
        RunState.model_validate(state), STAGE_PLAN)

    report = rundata.build_report(run_dir)
    resolved = {f["id"] for f in report["resolved"]}
    assert resolved and not resolved & {f["id"] for f in report["findings"]}
    assert f"| Нарушений найдено | {len(report['findings'])} |" in markdown
    verdict = report["verdict"]
    assert f"**{verdict['word']}.** {verdict['text']}" in markdown
    assert not set(verdict["first"]) & resolved
    row = next(r for r in rundata.list_runs(tmp_path / "runs") if r["run_id"] == run_dir.name)
    assert row["findings"] == len(report["findings"]) and row["critical"] == 0
    assert report["editor"]["after"] >= len(report["findings"])


def test_every_quote_is_located_inside_its_block(tmp_path):
    report = rundata.build_report(full_run(tmp_path))
    blocks = {b["id"]: b["text"] for b in report["document"]["blocks"]}

    evidence = [e for f in report["findings"] for e in f["evidence"]]
    assert evidence
    for e in evidence:
        assert e["verified"] is True
        start, end = e["span"]
        assert match_key(blocks[e["block_id"]][start:end]) == match_key(e["quote"])
        assert e["place"].startswith("строк")


def test_criteria_carry_the_standard_clause(tmp_path):
    report = rundata.build_report(full_run(tmp_path))

    assert report["criteria"]["unambiguous"]["standard"] == (
        "ISO/IEC/IEEE 29148:2018, п. 5.2.5; IEEE Std 830-1998, п. 4.3.2")
    assert report["criteria"]["traceable"]["standard"] == "IEEE Std 830-1998, п. 4.3.8"
    assert report["criteria"]["suspicious_content"]["standard"] is None


def test_progress_follows_the_trace(tmp_path):
    progress = rundata.progress(full_run(tmp_path))

    assert [s["code"] for s in progress["stages"]] == [code for code, _ in STAGE_PLAN]
    by_code = {s["code"]: s for s in progress["stages"]}
    # Nobody answered the agent's questions, so the re-check stage had nothing to do.
    assert by_code.pop("s8b")["status"] == "not_needed"
    assert all(s["status"] == "done" and s["seconds"] is not None for s in by_code.values())
    assert progress["finished"] and progress["status"] == "completed"
    assert progress["stages"][2]["llm_calls"] >= 1
    assert progress["stages"][0]["llm_calls"] == 0


def test_progress_of_a_run_in_flight_and_of_a_dead_one(tmp_path):
    run_dir = tmp_path / "20260101-000000-abcd"
    run_dir.mkdir()
    lines = [
        {"ts": "2026-01-01T00:00:00.000+05:00", "event": "run_start", "input": "a/spec.md",
         "profile": "quick"},
        {"ts": "2026-01-01T00:00:00.100+05:00", "event": "stage_start", "stage": "s0"},
        {"ts": "2026-01-01T00:00:00.200+05:00", "event": "stage_end", "stage": "s0",
         "seconds": 0.1},
        {"ts": "2026-01-01T00:00:00.300+05:00", "event": "stage_start", "stage": "s1"},
    ]
    text = "\n".join(json.dumps(line) for line in lines) + '\n{"ts": "2026-01-01T00:0'
    (run_dir / "trace.jsonl").write_text(text, encoding="utf-8")

    live = rundata.progress(run_dir, alive=True)
    dead = rundata.progress(run_dir, alive=False)

    assert live["status"] == "running" and live["input"] == "spec.md"
    assert [s["status"] for s in live["stages"][:3]] == ["done", "running", "pending"]
    assert dead["status"] == "interrupted"
    assert dead["stages"][1]["status"] == "interrupted"
    assert not live["has_report"]


def test_run_without_a_model_has_no_verdict_and_no_invented_numbers(tmp_path):
    main([str(ORGANIZERS_DOC), str(tmp_path / "report.md"), "--runs-dir", str(tmp_path / "runs")])
    run_dir = next((tmp_path / "runs").iterdir())

    report = rundata.build_report(run_dir)
    row = rundata.list_runs(tmp_path / "runs")[0]

    assert report["verdict"] is None and report["checks_ran"] is False
    assert report["findings"] == [] and report["errors"][0]["stage"] == "s2"
    assert len(report["document"]["blocks"]) == 124
    assert row["status"] == "partial" and row["checks_ran"] is False


# --- one-file report --------------------------------------------------------------------


def test_html_report_is_self_contained(tmp_path):
    run_dir = full_run(tmp_path)

    html = rundata.render_html(rundata.build_report(run_dir))

    data = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html,
                     re.S)
    embedded = json.loads(data.group(1))
    assert embedded["run_id"] == run_dir.name
    assert embedded["report_md"] == (run_dir / "report.md").read_text(encoding="utf-8")
    # Nothing is loaded from elsewhere: no scripts, styles, fonts or images by address.
    page = html.replace(data.group(0), "")
    assert not re.search(r'(src|href)\s*=\s*"(?!data:|#)', page)
    assert "@import" not in page
    assert not re.search(r'url\((?!"data:font/woff2;base64,)', page)
    # The fonts travel inside the file, every one the page declares.
    declared = re.findall(r'url\("fonts/([a-z0-9-]+\.woff2)"\)', rundata.page())
    assert declared and page.count('url("data:font/woff2;base64,') == len(declared)


def test_server_serves_only_the_shipped_fonts(tmp_path):
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1")
    declared = re.findall(r'url\("fonts/([a-z0-9-]+\.woff2)"\)', rundata.page())

    for name in declared:
        response = client.get(f"/fonts/{name}")
        assert response.status_code == 200 and response.headers["content-type"] == "font/woff2"
    assert client.get("/fonts/..%2Findex.html").status_code == 404
    assert client.get("/fonts/missing.woff2").status_code == 404


def test_html_data_cannot_break_out_of_its_script_tag(tmp_path):
    html = rundata.render_html({"note": "</script><script>alert(1)</script>"})

    assert html.count("</script>") == rundata.page().count("</script>") + 1
    assert "alert(1)" in html and "<script>alert(1)" not in html


def test_cli_html_flag_writes_the_file_next_to_the_report(tmp_path):
    report = tmp_path / "out" / "spec.report.md"

    main([str(ORGANIZERS_DOC), str(report), "--runs-dir", str(tmp_path / "runs"), "--html"])

    run_dir = next((tmp_path / "runs").iterdir())
    html = report.with_suffix(".html").read_text(encoding="utf-8")
    assert 'id="report-data"' in html
    assert (run_dir / "report.html").read_text(encoding="utf-8") == html


# --- server -----------------------------------------------------------------------------


def test_server_lists_runs_and_serves_the_report(tmp_path):
    run_dir = full_run(tmp_path)
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1")

    assert "<!--REPORT_DATA-->" in client.get("/").text
    assert [r["run_id"] for r in client.get("/api/runs").json()["runs"]] == [run_dir.name]
    report = client.get(f"/api/runs/{run_dir.name}/report").json()
    assert report["findings"] and report["verdict"]
    assert client.get(f"/api/runs/{run_dir.name}/progress").json()["finished"] is True
    download = client.get(f"/api/runs/{run_dir.name}/report.html")
    assert "attachment" in download.headers["content-disposition"]
    assert 'id="report-data"' in download.text
    assert client.get(f"/api/runs/{run_dir.name}/report.md").text.startswith("# Отчёт")


def test_server_never_sends_keys_and_rejects_foreign_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    (tmp_path / "runs").mkdir()
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1")

    config = client.get("/api/config")
    missing = client.get("/api/runs/..%2F..%2Fsecrets/report")
    unknown = client.get("/api/runs/20260101-000000-abcd/report")

    assert config.json()["model_ready"] is True
    assert "sk-secret-value" not in config.text
    assert missing.status_code == 404
    assert unknown.status_code == 404 and "не найден" in unknown.json()["error"]


def test_server_refuses_an_empty_upload_with_a_reason(tmp_path):
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1")

    response = client.post("/api/runs?name=spec.md&profile=quick", content=b"",
                           headers={"Content-Type": "application/octet-stream"})

    assert response.status_code == 400
    assert "Файл пустой" in response.json()["error"]


# --- the server spends the user's key: only its own page may start a run --------------

UPLOAD = {"Content-Type": "application/octet-stream"}


def test_server_answers_only_this_machine(tmp_path):
    (tmp_path / "runs").mkdir()
    app = create_app(tmp_path / "runs")
    assert TestClient(app, base_url="http://127.0.0.1:8000").get("/api/runs").status_code == 200
    assert TestClient(app, base_url="http://localhost:8000").get("/api/runs").status_code == 200
    rebound = TestClient(app, base_url="http://attacker.example").get("/api/runs")
    assert rebound.status_code == 421 and "127.0.0.1" in rebound.json()["error"]


def test_upload_from_a_foreign_page_is_refused(tmp_path):
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1:8000")
    url = "/api/runs?name=spec.md&profile=quick"
    as_form = client.post(url, content=b"x", headers={"Content-Type": "text/plain"})
    foreign = client.post(url, content=b"x",
                          headers={**UPLOAD, "Origin": "https://evil.example"})
    assert as_form.status_code == 403 and foreign.status_code == 403
    assert "только со страницы" in foreign.json()["error"]


def test_too_big_upload_is_refused_before_reading(tmp_path, monkeypatch):
    monkeypatch.setenv("REQAUDIT_MAX_INPUT_MB", "1")
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1")
    response = client.post("/api/runs?name=spec.md", content=b"x" * (2 * 1024 * 1024),
                           headers=UPLOAD)
    assert response.status_code == 413


def test_network_address_needs_an_explicit_flag(capsys):
    from reqaudit.ui.server import main

    assert main(["--host", "0.0.0.0", "--no-browser"]) == 2
    assert "--allow-network" in capsys.readouterr().err


def test_highlight_key_is_the_gate_key():
    """The per-character key used for highlighting must equal match_key, which the gates use."""
    from reqaudit.evidence import match_key, match_key_with_index

    samples = ["* **Ф-1.** Порог — «от 1 000 до 1 000 000 ₸»", "| Поле | Значение |",
               "Ёлка   и  ёж\tс `code` и > цитатой", "ﬁ лигатура и ½"]
    for text in samples:
        stream, index = match_key_with_index(text)
        assert stream == match_key(text) and len(index) == len(stream)


def test_author_answers_from_the_page_start_the_same_cli_recheck(tmp_path, monkeypatch):
    """The page posts answers; the server writes the answers file the CLI reads and starts
    `reqaudit --answers` on the same document. Foreign pages cannot do it."""
    from reqaudit.answers import parse
    from reqaudit.ui import server

    run_dir = full_run(tmp_path)
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    question = state["questions"][0]
    started: list[list[str]] = []

    class FakeRun:
        def __init__(self, args, **_):
            started.append(list(args))
            new = tmp_path / "runs" / "20990101-000000-beef"
            new.mkdir()
            (new / "trace.jsonl").write_text(json.dumps({"event": "run_start", "input": args[3]})
                                             + "\n", encoding="utf-8")

        def poll(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", FakeRun)
    client = TestClient(create_app(tmp_path / "runs"), base_url="http://127.0.0.1")
    url = f"/api/runs/{run_dir.name}/answers"
    body = {"answers": {question["id"]: "Лимит считается по календарным суткам Алматы."}}

    foreign = client.post(url, json=body, headers={"Origin": "https://evil.example"})
    empty = client.post(url, json={"answers": {question["id"]: "  "}})
    assert foreign.status_code == 403 and empty.status_code == 400

    response = client.post(url, json=body)
    assert response.status_code == 200 and response.json()["run_id"] == "20990101-000000-beef"
    args = started[-1]
    assert Path(args[3]) == ORGANIZERS_DOC.resolve() or Path(args[3]) == ORGANIZERS_DOC
    answers_file = Path(args[args.index("--answers") + 1])
    assert list(parse(answers_file.read_text(encoding="utf-8")).values()) == [
        "Лимит считается по календарным суткам Алматы."]
