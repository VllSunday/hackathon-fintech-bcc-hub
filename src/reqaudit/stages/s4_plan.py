"""s4 Plan: decide which checks apply to this document and split the work into cells.

Code only. The profile from s2 says what the document contains; this stage turns that
into cells. Nothing here knows the organisers' template: a check is added because the
document has the kind of content it needs, not because a section has a certain name.

The assumption sensor: the cells rely on the structure s2 recognised. When that
structure covers the document poorly (many blocks unclassified or skipped as "other",
or almost no requirements in a long text), the plan adds a general cell that reads the
whole document without relying on it, and the report says so. A familiar document gets
exactly the plan it got before.
"""

from __future__ import annotations

from reqaudit.criteria import Criterion as C
from reqaudit.models import Cell, CellKind, CellPlan, Coverage, PartRole, Unit
from reqaudit.stages.base import RunContext, Stage

BATCH_SIZE = 8

REQUIREMENT_CRITERIA = [C.UNAMBIGUOUS, C.VERIFIABLE, C.COMPLETE, C.SINGULAR, C.APPROPRIATE,
                        C.FEASIBLE, C.CONFORMING]
SET_CRITERIA = {
    CellKind.CONSISTENCY: [C.SET_CONSISTENT],
    CellKind.PARENT_CHILD: [C.SET_CONSISTENT, C.CORRECT, C.SET_COMPLETE],
    CellKind.ACCEPTANCE: [C.SET_VALIDATABLE, C.VERIFIABLE, C.SET_CONSISTENT, C.SET_COMPLETE],
    CellKind.COMPLETENESS: [C.SET_COMPLETE, C.SET_COMPREHENSIBLE, C.SET_FEASIBLE, C.NECESSARY,
                            C.RANKED, C.TRACEABLE, C.MODIFIABLE, C.DOCUMENT_INTEGRITY],
    CellKind.API_CONTRACT: [C.SET_CONSISTENT, C.COMPLETE, C.UNAMBIGUOUS, C.VERIFIABLE,
                            C.CONFORMING],
    CellKind.VALUES: [C.SET_CONSISTENT, C.UNAMBIGUOUS],
    CellKind.GENERAL: [C.UNAMBIGUOUS, C.VERIFIABLE, C.COMPLETE, C.SINGULAR, C.FEASIBLE,
                       C.SET_CONSISTENT, C.SET_COMPLETE, C.SET_COMPREHENSIBLE],
}
SET_TITLES = {
    CellKind.CONSISTENCY: "Непротиворечивость набора",
    CellKind.PARENT_CHILD: "Основной документ и дочерняя спецификация",
    CellKind.ACCEPTANCE: "Критерии приёмки",
    CellKind.COMPLETENESS: "Полнота и документ целиком",
    CellKind.API_CONTRACT: "Спецификация интерфейса",
    CellKind.VALUES: "Сверка значений",
    CellKind.GENERAL: "Общая проверка документа",
}

# Assumption sensor thresholds.
UNCLASSIFIED_SHARE = 0.10  # blocks s2 neither put into a requirement nor skipped
OTHER_SHARE = 0.25  # blocks skipped for no known reason
FEW_UNITS = 3  # so few requirements ...
LONG_TEXT = 15  # ... in a document with at least this many blocks


def assumption_check(coverage: Coverage | None, units: list[Unit]) -> list[str]:
    """Why the recognised structure may not fit the document; empty when it fits."""
    if coverage is None or coverage.blocks_checked == 0:
        return []
    total = coverage.blocks_checked
    reasons = []
    unclassified = len(coverage.unclassified)
    if unclassified >= 2 and unclassified / total >= UNCLASSIFIED_SHARE:
        reasons.append(f"не классифицировано {len(coverage.unclassified)} из {total} блоков")
    other = coverage.skipped_by_reason.get("other", 0)
    if other >= 2 and other / total >= OTHER_SHARE:
        reasons.append(f"{other} из {total} блоков пропущены без понятной причины")
    if len(units) < FEW_UNITS and total >= LONG_TEXT:
        reasons.append(f"в документе из {total} блоков выделено требований: {len(units)}")
    return reasons


def requirement_batches(units: list[Unit], size: int = BATCH_SIZE) -> list[list[Unit]]:
    """Consecutive units of the same part, at most `size` per batch."""
    batches: list[list[Unit]] = []
    for unit in units:
        if batches and len(batches[-1]) < size and batches[-1][-1].part == unit.part:
            batches[-1].append(unit)
        else:
            batches.append([unit])
    return batches


def plan_cells(ctx_units: list[Unit], has_child_spec: bool, has_acceptance: bool,
               has_api: bool, has_facts: bool = False,
               general_reasons: list[str] | None = None) -> CellPlan:
    cells: list[Cell] = []
    notes: list[str] = []

    for n, batch in enumerate(requirement_batches(ctx_units), 1):
        cells.append(Cell(id=f"req-{n:02d}", kind=CellKind.REQUIREMENTS,
                          title=f"Требования {batch[0].id}–{batch[-1].id}",
                          unit_ids=[u.id for u in batch], criteria=REQUIREMENT_CRITERIA))

    kinds = [CellKind.COMPLETENESS]
    if len(ctx_units) >= 2:
        kinds.insert(0, CellKind.CONSISTENCY)
    else:
        notes.append("Проверка непротиворечивости пропущена: в документе меньше двух требований.")
    if has_child_spec:
        kinds.append(CellKind.PARENT_CHILD)
    if has_acceptance:
        kinds.append(CellKind.ACCEPTANCE)
    else:
        notes.append("Критериев приёмки в документе нет: их отсутствие оценивается в проверке "
                     "полноты.")
    if has_api:
        kinds.append(CellKind.API_CONTRACT)
    if has_facts:
        kinds.append(CellKind.VALUES)
    if general_reasons:
        kinds.append(CellKind.GENERAL)
        notes.append("Структура документа распознана не полностью ("
                     + "; ".join(general_reasons) + "). Добавлена общая проверка: документ "
                     "читается целиком, без опоры на выделенные требования, включая блоки "
                     "вне них.")

    for kind in kinds:
        cells.append(Cell(id=kind.value, kind=kind, title=SET_TITLES[kind],
                          criteria=SET_CRITERIA[kind]))
    return CellPlan(cells=cells, notes=notes)


class PlanStage(Stage):
    code = "s4"
    title = "План ячеек"
    artifact = "s4_cells.json"

    def run(self, ctx: RunContext) -> CellPlan:
        profile = ctx.state.profile
        assert profile is not None, "s4 needs s2"
        plan = plan_cells(
            ctx.state.units,
            has_child_spec=any(p.role == PartRole.CHILD_SPEC for p in profile.parts),
            has_acceptance=profile.has_acceptance_criteria,
            has_api=profile.has_api_spec,
            has_facts=bool(ctx.state.facts and ctx.state.facts.facts),
            general_reasons=assumption_check(ctx.state.coverage, ctx.state.units),
        )
        ctx.state.cells = plan.cells
        ctx.state.plan_notes = plan.notes
        ctx.trace.event("plan_summary", stage=self.code,
                        cells=[c.id for c in plan.cells], notes=plan.notes)
        return plan
