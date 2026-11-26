"""s8b Re-check after the author's answers.

Runs only when there are answers: from a file (`--answers`) or asked in the terminal
(`--ask`). For each answered question, a reviewer re-checks the findings it concerns and
decides: the defect stays and the answer becomes a concrete fix of the document's text
(the usual case: an answer outside the document does not change the document), the
finding was wrong after all, or its severity changes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

from reqaudit import prompt_store
from reqaudit.answers import fingerprint
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.models import CheckFailure, Finding, Question, Severity
from reqaudit.reading.render import LEGEND
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import document_view

BATCH_SIZE = 8


class Reassessment(BaseModel):
    finding_id: str
    decision: Literal["confirmed", "resolved", "changed"]
    reasoning: str
    document_fix: str | None = None
    severity: Severity | None = None

    @field_validator("decision", mode="before")
    @classmethod
    def _unclear_is_confirmed(cls, value: object) -> object:
        # When in doubt the defect stays: an unclear decision must not hide it.
        return value if value in ("confirmed", "resolved", "changed") else "confirmed"

    @field_validator("severity", mode="before")
    @classmethod
    def _invalid_is_none(cls, value: object) -> object:
        return value if value in {s.value for s in Severity} else None


class ReassessAnswer(BaseModel):
    reassessments: list[Reassessment]


class AnswerResult(BaseModel):
    """Artifact of s8b."""

    answered_questions: list[str]
    unmatched_answers: int
    decisions: dict[str, str]


def _check(finding_ids: list[str]) -> Check:
    def check(answer: ReassessAnswer) -> str | None:
        missing = [i for i in finding_ids if i not in {r.finding_id for r in
                                                       answer.reassessments}]
        if missing:
            return f"Give a decision for every listed finding; missing {missing}."
        return None

    return combine(check, require_russian("reassessments.reasoning",
                                          "reassessments.document_fix"))


def _listing(question: Question, findings: list[Finding]) -> str:
    items = []
    for f in findings:
        quotes = "\n".join(f"    - [{e.block_id}] «{e.quote}»" for e in f.evidence)
        items.append(f"  {f.id}: {f.title} (criterion={f.criterion.value}, "
                     f"severity={f.severity.value})\n    {f.explanation}\n{quotes}")
    return (f"{question.id}: {question.text}\n  Author's answer: {question.answer}\n"
            + "\n".join(items))


class AnswersStage(Stage):
    code = "s8b"
    title = "Перепроверка по ответам автора"
    artifact = "s8b_answers.json"

    def applies(self, ctx: RunContext) -> bool:
        return bool(ctx.state.questions) and (bool(ctx.answers) or ctx.ask is not None)

    def run(self, ctx: RunContext) -> AnswerResult:
        state = ctx.state
        if ctx.ask is not None and not ctx.answers:
            ctx.answers = self._ask(ctx)

        answered: list[Question] = []
        questions = []
        for q in state.questions:
            text = ctx.answers.get(fingerprint(q.text))
            if text:
                q = q.model_copy(update={"answer": text})
                answered.append(q)
            questions.append(q)
        state.questions = questions
        used = {fingerprint(q.text) for q in answered}
        unmatched = len([k for k in ctx.answers if k not in used])

        by_id = {f.id: f for f in state.findings}
        decisions: dict[str, str] = {}
        if answered:
            instructions, ids = prompt_store.compose("s8b_reassess", legend=LEGEND)
            context = document_view(state.document, {b.id for b in state.document.blocks},
                                    ctx.settings.window_chars)
            batches = [answered[i:i + BATCH_SIZE] for i in range(0, len(answered), BATCH_SIZE)]
            requests = []
            for n, batch in enumerate(batches, 1):
                finding_ids = [i for q in batch for i in q.finding_ids if i in by_id]
                listing = "\n\n".join(_listing(q, [by_id[i] for i in q.finding_ids
                                                   if i in by_id]) for q in batch)
                requests.append(dict(
                    step=f"s8b_reassess[{n}]", role="judge", instructions=instructions,
                    prompt=f"{context}\n\n<answered>\n{listing}\n</answered>\n",
                    output_type=ReassessAnswer, prompt_ids=ids, check=_check(finding_ids)))
            for batch, result in zip(batches, ctx.llm.many(requests), strict=True):
                if isinstance(result, LLMError):
                    state.check_failures.append(CheckFailure(
                        stage=self.code, cell="перепроверка по ответам автора",
                        message=result.message))
                    continue
                answers = {i: q.answer for q in batch for i in q.finding_ids}
                for r in result.reassessments:
                    finding = by_id.get(r.finding_id)
                    if finding is None or r.finding_id not in answers:
                        continue
                    decisions[r.finding_id] = r.decision
                    by_id[r.finding_id] = finding.model_copy(update={
                        "author_answer": answers[r.finding_id],
                        "answer_decision": r.decision,
                        "answer_note": r.reasoning.strip(),
                        "document_fix": (r.document_fix or "").strip() or None,
                        "severity": (r.severity if r.decision == "changed" and r.severity
                                     else finding.severity),
                        "trail": [*finding.trail, f"author:{r.decision}"],
                    })
        state.findings = [by_id[f.id] for f in state.findings]
        ctx.trace.event("answers_summary", stage=self.code, answered=len(answered),
                        unmatched=unmatched, decisions=decisions)
        return AnswerResult(answered_questions=[q.id for q in answered],
                            unmatched_answers=unmatched, decisions=decisions)

    @staticmethod
    def _ask(ctx: RunContext) -> dict[str, str]:
        """Ask every question in the terminal; an empty line skips a question."""
        answers = {}
        total = len(ctx.state.questions)
        for n, q in enumerate(ctx.state.questions, 1):
            prompt = (f"\n[{n}/{total}] {q.id} · {q.addressee}\n{q.text}\n"
                      "Ответ (Enter — пропустить): ")
            text = (ctx.ask(prompt) or "").strip()
            if text:
                answers[fingerprint(q.text)] = text
        return answers
