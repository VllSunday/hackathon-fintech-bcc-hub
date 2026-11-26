"""The bench: variants parsed, runs measured against gold, the whole loop without a model."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import fake_team

from reqaudit.bench import BenchRun, main, measure, parse_variant, plan, summary
from reqaudit.config import Settings
from reqaudit.pipeline import run_pipeline

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"
NO_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "VERTEX_API_KEY",
           "ANTHROPIC_API_KEY", "REQAUDIT_MODEL_JUDGE")


def test_variants_are_names_with_reqaudit_settings():
    v = parse_variant("lean:REQAUDIT_THINKING_ANALYST=low, REQAUDIT_WARM_UP=off")
    assert v.name == "lean" and v.env == {"REQAUDIT_THINKING_ANALYST": "low",
                                          "REQAUDIT_WARM_UP": "off"}
    assert parse_variant("base").env == {}
    with pytest.raises(ValueError):
        parse_variant("bad:OPENAI_API_KEY=x")  # a variant never carries keys
    runs = plan([Path("a.md"), Path("b.md")], [parse_variant("x"), parse_variant("y")], 2)
    assert len(runs) == 8 and {r.repeat for r in runs} == {0, 1}


def test_a_run_is_scored_against_its_gold_and_diagnosed(tmp_path):
    corpus = tmp_path / "corpus"
    (corpus / "inputs" / "near").mkdir(parents=True)
    (corpus / "gold").mkdir()
    doc = corpus / "inputs" / "near" / "spec.md"
    shutil.copy(ORGANIZERS_DOC, doc)
    (corpus / "gold" / "spec.yaml").write_text(
        "document: near/spec.md\ndefects:\n  - id: G-01\n    scope: requirement\n"
        "    criterion: verifiable\n    severity: major\n    quotes:\n"
        "      - \"Месяц определяется по дате совершения покупки в Алматы.\"\n"
        "traps:\n  - id: T-01\n    quotes:\n      - \"Мобильное приложение → сервис настроек.\"\n",
        encoding="utf-8")
    model, _ = fake_team()
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(doc, tmp_path / "r.md", settings, model_factory=lambda _id: model)

    run = measure(BenchRun(variant="base", document=str(doc), repeat=0,
                           run_dir=str(result.run_dir)), corpus)
    assert run.status == "completed" and run.findings and run.findings > 0
    assert run.recall is not None and run.traps.endswith("/1") and run.warnings is not None
    assert "| base | spec.md | 1 | completed |" in summary([run])


def test_the_bench_runs_the_real_command_without_spending(tmp_path, monkeypatch):
    """No key in the environment: the agent reads the document and calls no model."""
    for key in NO_KEYS:
        monkeypatch.setenv(key, "")
    out = tmp_path / "bench"
    assert main([str(ORGANIZERS_DOC), "--variant", "base", "--variant",
                 "lean:REQAUDIT_THINKING_ANALYST=low", "--parallel", "2",
                 "--out", str(out)]) == 0
    text = (out / "summary.md").read_text(encoding="utf-8")
    assert "| base | requirements.md | 1 |" in text and "| lean | requirements.md | 1 |" in text
    assert "| 0.00 |" in text  # nothing was spent
    assert (out / "results.json").exists()
