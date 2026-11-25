"""s7 Evidence gates: code decides what may enter the report.

Gates are computed, never asserted by a model:
- every quote must be found in the document; unfound quotes are removed, and a finding
  left without evidence is rejected;
- a conflict must quote at least two different statements;
- requirement ids must exist; missing ones are taken from the blocks the quotes come from;
- duplicates found by different cells are merged;
- defects of the file itself found while reading (hidden text, broken markup) are added.
"""

from __future__ import annotations

from pydantic import BaseModel

from reqaudit import criteria
from reqaudit.criteria import Criterion, Level
from reqaudit.evidence import match_key
from reqaudit.models import Evidence, Finding, FindingStatus, Scope, Severity
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import EvidenceDraft, verify_evidence

SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.MAJOR: 1, Severity.MINOR: 2}
_ISSUE_FINDINGS = {
    "hidden_text": (Criterion.SUSPICIOUS_CONTENT, Severity.MAJOR,
                    "В документе есть скрытый текст",
                    "Фрагмент не виден читателю документа, но читается программами. Скрытый "
                    "текст может содержать указания для автоматических проверок или устаревшие "
                    "требования, которые никто не согласовывал."),
    "unclosed_code_block": (Criterion.DOCUMENT_INTEGRITY, Severity.MINOR,
                            "Незакрытый блок кода ломает разметку документа",
                            "Блок кода не закрыт, поэтому часть документа может отображаться "
                            "не так, как задумал автор."),
    "table_column_mismatch": (Criterion.DOCUMENT_INTEGRITY, Severity.MINOR,
                              "Число ячеек в строке таблицы не совпадает с заголовком",
                              "Строка таблицы не совпадает с её заголовком: значение может "
                              "оказаться не в той колонке и быть прочитано неверно."),
}


class GateResult(BaseModel):
    """Artifact of s7."""

    findings: list[Finding]
    rejected: list[Finding]
    stats: dict[str, int]


def _scope(criterion: Criterion, unit_ids: list[str]) -> Scope:
    level = criteria.CRITERIA[criterion].level
    if level == Level.REQUIREMENT:
        return Scope.REQUIREMENT
    if level == Level.DOCUMENT:
        return Scope.DOCUMENT
    return Scope.PAIR if criterion == Criterion.SET_CONSISTENT and len(unit_ids) == 2 \
        else Scope.SET


def _same_defect(a: Finding, b: Finding) -> bool:
    blocks_a = {e.block_id for e in a.evidence}
    blocks_b = {e.block_id for e in b.evidence}
    overlap = len(blocks_a & blocks_b)
    if not overlap:
        return False
    if blocks_a == blocks_b and a.criterion == b.criterion:
        return True
    smaller = min(len(blocks_a), len(blocks_b))
    return a.criterion == b.criterion and overlap / smaller >= 0.5 and len(blocks_a) > 1


def merge_duplicates(findings: list[Finding]) -> tuple[list[Finding], int]:
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])
    kept: list[Finding] = []
    merged = 0
    for f in ordered:
        twin = next((k for k in kept if _same_defect(k, f)), None)
        if twin is None:
            kept.append(f)
            continue
        merged += 1
        index = kept.index(twin)
        kept[index] = twin.model_copy(update={
            "unit_ids": sorted(set(twin.unit_ids) | set(f.unit_ids)),
            "trail": [*twin.trail, f"merged:{f.id}"],
            "question": twin.question or f.question,
        })
    return kept, merged


class GatesStage(Stage):
    code = "s7"
    title = "Гейты доказательств"
    artifact = "s7_gated.json"

    def run(self, ctx: RunContext) -> GateResult:
        state = ctx.state
        document = state.document
        blocks = {b.id: b for b in document.blocks if not b.hidden}
        order = {b.id: n for n, b in enumerate(document.blocks)}
        unit_ids = {u.id for u in state.units}
        units_of_block: dict[str, list[str]] = {}
        for u in state.units:
            for b in u.block_ids:
                units_of_block.setdefault(b, []).append(u.id)

        passed: list[Finding] = []
        rejected: list[Finding] = []
        stats = {"in": len(state.findings), "no_evidence": 0, "one_sided_conflict": 0}
        for f in state.findings:
            evidence = [v for e in f.evidence
                        if (v := verify_evidence(EvidenceDraft(block_id=e.block_id,
                                                               quote=e.quote), blocks))]
            if not evidence:
                stats["no_evidence"] += 1
                rejected.append(f.model_copy(update={
                    "status": FindingStatus.REJECTED, "trail": [*f.trail, "gate:no_evidence"],
                    "review_note": "Ни одна цитата не найдена в документе."}))
                continue
            distinct = {(e.block_id, match_key(e.quote)) for e in evidence}
            if f.criterion == Criterion.SET_CONSISTENT and len(distinct) < 2:
                stats["one_sided_conflict"] += 1
                rejected.append(f.model_copy(update={
                    "status": FindingStatus.REJECTED, "trail": [*f.trail, "gate:one_sided"],
                    "review_note": "Противоречие подтверждено цитатой только одной стороны."}))
                continue
            units = [u for u in f.unit_ids if u in unit_ids]
            if not units:
                units = sorted({u for e in evidence for u in units_of_block.get(e.block_id, [])})
            passed.append(f.model_copy(update={
                "evidence": evidence, "unit_ids": units, "scope": _scope(f.criterion, units),
                "trail": [*f.trail, "gate:passed"]}))

        merged, stats["merged"] = merge_duplicates(passed)
        merged.extend(self._file_defects(ctx))
        merged.sort(key=lambda f: (SEVERITY_ORDER[f.severity],
                                   min(order.get(e.block_id, 10**6) for e in f.evidence)))
        final = [f.model_copy(update={"id": f"D-{n:03d}"}) for n, f in enumerate(merged, 1)]
        stats["out"] = len(final)

        state.findings = final
        state.rejected = [*state.rejected, *rejected]
        ctx.trace.event("gates_summary", stage=self.code, **stats)
        return GateResult(findings=final, rejected=rejected, stats=stats)

    @staticmethod
    def _file_defects(ctx: RunContext) -> list[Finding]:
        """Findings about the file itself, from what the reader recorded."""
        document = ctx.state.document
        blocks = {b.id: b for b in document.blocks}
        found = []
        for issue in document.issues:
            spec = _ISSUE_FINDINGS.get(issue.kind)
            block = blocks.get(issue.block_id or "")
            if spec is None or block is None:
                continue
            criterion, severity, title, explanation = spec
            quote = block.text.split("\n")[0][:200]
            if issue.kind == "hidden_text":
                short = quote if len(quote) <= 50 else quote[:50].rsplit(" ", 1)[0] + "…"
                title = f"Скрытый текст: «{short}»"
            found.append(Finding(
                id=f"read#{len(found) + 1}", scope=_scope(criterion, []), criterion=criterion,
                title=title, explanation=f"{explanation} {issue.message}",
                evidence=[Evidence(block_id=block.id, quote=quote, verified=True)],
                severity=severity, status=FindingStatus.CONFIRMED, trail=["reader"],
                suggestion="Удалить или сделать видимым фрагмент и согласовать его содержание."
                if issue.kind == "hidden_text" else "Исправить разметку документа."))
        return found
