"""s7 Evidence gates: code decides what may enter the report.

Gates are computed, never asserted by a model:
- every quote must be found in the document; unfound quotes are removed, and a finding
  left without evidence is rejected;
- a conflict must quote at least two different statements;
- requirement ids must exist; missing ones are taken from the blocks the quotes come from;
- duplicates found by different cells are merged;

The same checks already ran before the opponent (s6), so the opponent was not paid to
judge unfound quotes and duplicates; here they run once more on what the review kept.
A conflict with one side left is dropped only here, after the reviewer and the opponent
had the chance to quote the other side or to file it as a gap.
- defects of the file itself found while reading (hidden text, broken markup) are added.
"""

from __future__ import annotations

from pydantic import BaseModel

from reqaudit.criteria import Criterion
from reqaudit.models import Evidence, Finding, FindingStatus, Severity, scope_of
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import merge_duplicates, screen

__all__ = ["GateResult", "GatesStage", "merge_duplicates"]

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


class GatesStage(Stage):
    code = "s7"
    title = "Гейты доказательств"
    artifact = "s7_gated.json"

    def run(self, ctx: RunContext) -> GateResult:
        state = ctx.state
        document = state.document
        blocks = {b.id: b for b in document.blocks if not b.hidden}
        order = {b.id: n for n, b in enumerate(document.blocks)}

        screened = screen(state.findings, blocks, state.units)
        passed = [f.model_copy(update={"trail": [*f.trail, "gate:passed"]})
                  for f in screened.passed]
        rejected, stats = screened.rejected, {"in": len(state.findings), **screened.stats}

        merged, stats["merged"] = merge_duplicates(passed)
        merged.extend(self._file_defects(ctx))
        merged.sort(key=lambda f: (f.severity.rank,
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
                id=f"read#{len(found) + 1}", scope=scope_of(criterion, []), criterion=criterion,
                title=title, explanation=f"{explanation} {issue.message}",
                evidence=[Evidence(block_id=block.id, quote=quote, verified=True)],
                severity=severity, status=FindingStatus.CONFIRMED, trail=["reader"],
                suggestion="Удалить или сделать видимым фрагмент и согласовать его содержание."
                if issue.kind == "hidden_text" else "Исправить разметку документа."))
        return found
