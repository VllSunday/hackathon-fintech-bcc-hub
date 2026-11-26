"""The assumption sensor: when the recognised structure covers the document poorly, the
plan adds a general cell that reads the whole document, and the report says why.
A familiar document gets exactly the plan it got before."""

import json
from dataclasses import replace
from pathlib import Path

from fakes import fake_team, honest_segmentation

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


def weak_segmenter(prompt: str) -> dict:
    """Misses obligations stated in paragraphs, as a weak model would."""
    return honest_segmentation(prompt, honest_paragraphs=False)


def run(tmp_path: Path, document: Path, segmenter=honest_segmentation):
    model, calls = fake_team(segmenter=segmenter)
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(document, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    return result, state, (tmp_path / "report.md").read_text(encoding="utf-8")


def test_unfamiliar_document_takes_the_general_path(tmp_path):
    document = tmp_path / "prose.md"
    document.write_text(PROSE, encoding="utf-8")
    result, state, report = run(tmp_path, document, weak_segmenter)

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


SHORT = """# Требования к журналу

- Сервис пишет в журнал каждое уведомление.
- Запись журнала содержит идентификатор карты.

Система должна хранить журнал не менее пяти лет.

Документ подготовлен отделом цифровых продуктов.
"""


def test_second_segmenter_catches_a_missed_obligation(tmp_path):
    """A short document: only the code cross-check can see the paragraph the model missed."""
    document = tmp_path / "short.md"
    document.write_text(SHORT, encoding="utf-8")
    _, honest, _ = run(tmp_path / "honest", document)
    assert honest["coverage"]["obligations_outside"] == {}
    assert "general" not in [c["id"] for c in honest["cells"]]

    result, state, report = run(tmp_path / "weak", document, weak_segmenter)
    assert result.exit_code == 0, report
    assert list(state["coverage"]["obligations_outside"].values()) == ["должна"]
    assert "**Сверка вторым способом:** 1 блок с явным обязательством" in report
    assert "general" in [c["id"] for c in state["cells"]]
    general = [f for f in state["findings"] if f["cell"] == "general"]
    assert general[0]["evidence"][0]["quote"].startswith("Система должна хранить журнал")



# --- focus checks: the profiler adds checks this document needs ---------------------------


def test_profiler_focus_checks_become_cells_within_the_frame(tmp_path):
    def focus(first_block):
        return [
            {"title": "Жизненный цикл статусов", "instruction": "Сверить переходы статусов "
             "заявки с критериями приёмки.", "criteria": ["set_consistent", "made_up"],
             "start_block_ids": [first_block, "b9999"]},
            {"title": "Без критериев", "instruction": "Что-то проверить.",
             "criteria": ["made_up"], "start_block_ids": []},
        ]

    model, _ = fake_team(focus=focus)
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(ORGANIZERS_DOC, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    focus_cells = [c for c in state["cells"] if c["kind"] == "focus"]
    assert [c["title"] for c in focus_cells] == ["Жизненный цикл статусов"]
    assert focus_cells[0]["criteria"] == ["set_consistent"]  # unknown codes dropped
    assert "b9999" not in focus_cells[0]["block_ids"]
    assert "unknown criteria" in trace  # the profiler was asked to fix them first
    assert '"s5_focus@' in trace
    assert "«Жизненный цикл статусов»" in report



# --- not a specification; the call budget follows the plan --------------------------------

MARKETING = """# Карта «Путешествие»

Карту можно оформить в приложении за пару минут и забрать в любом отделении.

До 7 % возвращаем бонусами за билеты и отели. Бонусы тратятся на новые поездки.

- Бесплатные проходы в бизнес-залы на тарифе «Премиум».
- Страховка для поездок за рубеж в один клик.
"""
NOT_SPEC = {"is_specification": False, "not_specification_reason": "рекламный текст о карте",
            "has_acceptance_criteria": False, "has_api_spec": False,
            "has_data_requirements": False, "has_non_functional": False, "has_ui_spec": False}
# The profile itself sees acceptance criteria: «not a specification» contradicts it.
NOT_SPEC_BUT_SEES = {**NOT_SPEC, "has_acceptance_criteria": True}


def run_doc(tmp_path: Path, document: Path, **team):
    model, calls = fake_team(**team)
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(document, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
    return result, state, (tmp_path / "report.md").read_text(encoding="utf-8"), trace, calls


def test_marketing_text_is_reported_as_not_a_specification(tmp_path):
    document = tmp_path / "ad.md"
    document.write_text(MARKETING, encoding="utf-8")
    result, state, report, _, calls = run_doc(tmp_path, document, profile=NOT_SPEC)
    assert result.exit_code == 0
    assert calls["segment"] == 0 and calls["review"] == 0 and calls["facts"] == 0
    assert state["units"] == [] and state["cells"] == [] and state["findings"] == []
    assert "**Не применимо.** Это не спецификация требований: рекламный текст о карте" in report


def test_explicit_obligations_overrule_not_a_specification(tmp_path):
    _, state, _, trace, calls = run_doc(tmp_path, ORGANIZERS_DOC, profile=NOT_SPEC_BUT_SEES)
    assert '"specification_overruled"' in trace
    assert calls["segment"] >= 1 and state["units"] and state["cells"]


def test_call_budget_grows_with_the_plan_unless_the_user_fixed_it(tmp_path):
    model, _ = fake_team()
    small = replace(Settings(), runs_dir=tmp_path / "a", cache_dir=tmp_path / "cache",
                    models={"judge": ["fake:team"]}, max_llm_calls=4)
    grown = run_pipeline(ORGANIZERS_DOC, tmp_path / "a.md", small, model_factory=lambda _id: model)
    assert grown.exit_code == 0
    assert '"budget_raised"' in (grown.run_dir / "trace.jsonl").read_text(encoding="utf-8")

    fixed = replace(small, runs_dir=tmp_path / "b", cache_dir=tmp_path / "cache-b",
                    max_llm_calls_fixed=True)
    capped = run_pipeline(ORGANIZERS_DOC, tmp_path / "b.md", fixed,
                          model_factory=lambda _id: model)
    assert capped.exit_code != 0  # the user's cap is respected
