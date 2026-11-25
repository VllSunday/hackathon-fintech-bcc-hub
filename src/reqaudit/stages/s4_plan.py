"""s4 Plan: decide which checks apply to this document and split the work into cells.

Code only. The profile from s2 says what the document contains; this stage turns that
into cells. Nothing here knows the organisers' template: a check is added because the
document has the kind of content it needs, not because a section has a certain name.
"""

from __future__ import annotations

from reqaudit.criteria import Criterion as C
from reqaudit.models import Cell, CellKind, CellPlan, PartRole, Unit
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
}
SET_TITLES = {
    CellKind.CONSISTENCY: "Непротиворечивость набора",
    CellKind.PARENT_CHILD: "Основной документ и дочерняя спецификация",
    CellKind.ACCEPTANCE: "Критерии приёмки",
    CellKind.COMPLETENESS: "Полнота и документ целиком",
    CellKind.API_CONTRACT: "Спецификация интерфейса",
}


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
               has_api: bool) -> CellPlan:
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
        )
        ctx.state.cells = plan.cells
        ctx.trace.event("plan_summary", stage=self.code,
                        cells=[c.id for c in plan.cells], notes=plan.notes)
        return plan
