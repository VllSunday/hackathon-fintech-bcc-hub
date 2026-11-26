"""Diagnosis of a run from its files: spend, model trouble, losses on the gates."""

from dataclasses import replace
from pathlib import Path

from fakes import fake_team, honest_set_review

from reqaudit.config import Settings
from reqaudit.diagnose import diagnose, render
from reqaudit.pipeline import run_pipeline

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def run(tmp_path: Path, **team) -> Path:
    model, _ = fake_team(**team)
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    return run_pipeline(ORGANIZERS_DOC, tmp_path / "report.md", settings,
                        model_factory=lambda _id: model).run_dir


def test_a_clean_run_reads_as_completed_with_its_path_of_findings(tmp_path):
    d = diagnose(run(tmp_path))
    text = render(d)
    assert d.status == "completed"
    assert "### Путь находок" in text and "Кандидатов от рецензентов" in text
    assert "### Нарезка" in text


def test_a_gap_filed_as_a_conflict_is_reported_as_lost(tmp_path):
    def gap_as_conflict(prompt):
        answer = honest_set_review(prompt)
        for f in answer["findings"]:
            if f["criterion"] == "set_consistent":
                f["evidence"] = f["evidence"][:1]
                f["title"] = "Не указан часовой пояс"
        return answer

    d = diagnose(run(tmp_path, set_review=gap_as_conflict))
    assert any("Не указан часовой пояс" in w and "критерий выбран неверно" in w
               for w in d.warnings)
