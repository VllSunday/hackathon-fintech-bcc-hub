"""The assumption sensor: when the recognised structure covers the document poorly, the
plan adds a general cell that reads the whole document, and the report says why.
A familiar document gets exactly the plan it got before."""

import json
from dataclasses import replace
from pathlib import Path

from fakes import fake_team

from reqaudit.config import Settings
from reqaudit.models import CellKind, Coverage, Unit, UnitKind
from reqaudit.pipeline import run_pipeline
from reqaudit.stages.s4_plan import assumption_check, plan_cells

ORGANIZERS_DOC = Path(__file__).parents[1] / "corpus" / "inputs" / "organizers" / "requirements.md"


def units(n: int) -> list[Unit]:
    return [Unit(id=f"R-{i:03d}", kind=UnitKind.FUNCTIONAL, block_ids=[f"b{i:04d}"],
                 quote="q", summary="s") for i in range(1, n + 1)]


def coverage(total: int, in_units: int, unclassified: int = 0, other: int = 0) -> Coverage:
    return Coverage(blocks_checked=total, in_units=in_units,
                    skipped=total - in_units - unclassified,
                    unclassified=[f"b{9000 + i}" for i in range(unclassified)],
                    skipped_by_reason={"context": total - in_units - unclassified - other,
                                       "other": other})


def test_sensor_is_quiet_when_the_structure_fits():
    assert assumption_check(coverage(40, 30, unclassified=1, other=2), units(20)) == []
    assert assumption_check(coverage(6, 1), units(1)) == []  # a short note, not a long text
    assert assumption_check(None, []) == []


def test_sensor_names_every_reason():
    reasons = assumption_check(coverage(40, 10, unclassified=6, other=12), units(5))
    assert len(reasons) == 2
    assert "не классифицировано 6 из 40 блоков" in reasons[0]
    assert "12 из 40 блоков пропущены без понятной причины" in reasons[1]
    assert assumption_check(coverage(20, 1), units(1)) == [
        "в документе из 20 блоков выделено требований: 1"]


def test_general_cell_is_added_only_on_the_general_path():
    familiar = plan_cells(units(5), has_child_spec=False, has_acceptance=True, has_api=False)
    assert CellKind.GENERAL not in {c.kind for c in familiar.cells}

    general = plan_cells(units(5), has_child_spec=False, has_acceptance=True, has_api=False,
                         general_reasons=["не классифицировано 6 из 40 блоков"])
    assert general.cells[-1].kind == CellKind.GENERAL
    assert [c.id for c in general.cells[:-1]] == [c.id for c in familiar.cells]
    assert "не классифицировано 6 из 40 блоков" in general.notes[-1]


# --- in the pipeline --------------------------------------------------------------------

PROSE = "\n\n".join(
    ["# Пояснительная записка к сервису уведомлений"]
    + [f"Абзац {n}. Сервис должен уведомлять клиента о событии номер {n} в течение "
       f"{n + 1} минут." for n in range(1, 17)]
    + ["- Клиент задаёт порог расходов в тенге."])


def run(tmp_path: Path, document: Path):
    model, calls = fake_team()
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(document, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    return result, state, (tmp_path / "report.md").read_text(encoding="utf-8")


def test_unfamiliar_document_takes_the_general_path(tmp_path):
    document = tmp_path / "prose.md"
    document.write_text(PROSE, encoding="utf-8")
    result, state, report = run(tmp_path, document)

    assert result.exit_code == 0, report
    assert "general" in [c["id"] for c in state["cells"]]
    assert "Структура документа распознана не полностью" in report
    general = [f for f in state["findings"] if f["cell"] == "general"]
    assert general and general[0]["unit_ids"] == []
    assert general[0]["evidence"][0]["quote"].startswith("Абзац 1.")


def test_organizers_document_keeps_the_usual_plan(tmp_path):
    _, state, report = run(tmp_path, ORGANIZERS_DOC)
    assert "general" not in [c["id"] for c in state["cells"]]
    assert "распознана не полностью" not in report
