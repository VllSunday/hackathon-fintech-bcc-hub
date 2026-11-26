"""s8 Editor: merge findings that describe one defect, and consolidate questions.

The gates (s7) merge duplicates code can see: same criterion, same places. Two findings
about one defect under different criteria need judgement, so a model groups them here.
Code then checks the answer: groups refer to existing findings, and every critical and
major finding is covered by a question. If the editor is unavailable, the findings stay
as the gates left them and questions are built from the reviewers' own questions.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from reqaudit import prompt_store
from reqaudit.evidence import match_key
from reqaudit.llm.checks import Check, combine, require_russian
from reqaudit.llm.client import LLMError
from reqaudit.models import CheckFailure, Finding, MergedFinding, Question, Severity
from reqaudit.reading.render import LEGEND
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import QuestionTo, document_view

ADDRESSEE_RU = {
    "analyst": "аналитик",
    "product_owner": "владелец продукта",
    "architect": "архитектор",
    "developer": "разработчик",
    "tester": "тестировщик",
    "security": "информационная безопасность",
}
_PRIORITY = {Severity.CRITICAL: 1, Severity.MAJOR: 2, Severity.MINOR: 3}
_MAX_EVIDENCE = 4


class DuplicateGroup(BaseModel):
    finding_ids: list[str]
    keep: str
    reason: str


class QuestionDraft(BaseModel):
    text: str
    finding_ids: list[str]
    question_to: QuestionTo | None = None

    @field_validator("question_to", mode="before")
    @classmethod
    def _unknown_is_none(cls, value: object) -> object:
        return value if value in ADDRESSEE_RU else None


class EditorAnswer(BaseModel):
    duplicates: list[DuplicateGroup]
    questions: list[QuestionDraft]


class Edited(BaseModel):
    """Artifact of s8."""

    findings: list[Finding]
    questions: list[Question]
    merged_groups: list[DuplicateGroup]
    editor_used: bool


def editor_check(findings: list[Finding]) -> Check:
    known = {f.id for f in findings}
    must_cover = {f.id for f in findings if f.severity in (Severity.CRITICAL, Severity.MAJOR)}

    def check(answer: EditorAnswer) -> str | None:
        complaints = []
        seen: set[str] = set()
        for n, group in enumerate(answer.duplicates):
            unknown = [i for i in group.finding_ids if i not in known]
            if unknown:
                complaints.append(f"duplicates[{n}]: unknown finding ids {unknown}")
            if group.keep not in group.finding_ids:
                complaints.append(f"duplicates[{n}]: keep {group.keep!r} is not in the group")
            if len(set(group.finding_ids)) < 2:
                complaints.append(f"duplicates[{n}]: a group needs at least two findings")
            if overlap := seen & set(group.finding_ids):
                complaints.append(f"duplicates[{n}]: {sorted(overlap)} already in another group")
            seen |= set(group.finding_ids)
        absorbed = {i for g in answer.duplicates for i in g.finding_ids if i != g.keep}
        covered = {i for q in answer.questions for i in q.finding_ids}
        for n, q in enumerate(answer.questions):
            unknown = [i for i in q.finding_ids if i not in known]
            if unknown:
                complaints.append(f"questions[{n}]: unknown finding ids {unknown}")
        missing = sorted(must_cover - absorbed - covered)
        if missing:
            complaints.append(f"critical and major findings without a question: {missing}")
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in complaints)) if complaints else None

    return combine(check, require_russian("duplicates.reason", "questions.text"))


def _listing(findings: list[Finding]) -> str:
    items = []
    for f in findings:
        quotes = "; ".join(f"[{e.block_id}] «{e.quote[:160]}»" for e in f.evidence)
        items.append(f"{f.id}: criterion={f.criterion.value} severity={f.severity.value} "
                     f"requirements={','.join(f.unit_ids) or '—'}\n  title: {f.title}\n"
                     f"  evidence: {quotes}\n  reviewer's question: {f.question or '—'}")
    return "\n\n".join(items)


def apply_groups(findings: list[Finding],
                 groups: list[DuplicateGroup]) -> tuple[list[Finding], dict[str, str]]:
    """Merge each valid group into its kept finding: strongest severity, all requirements.

    Returns the findings and where each absorbed id went, so questions can follow.
    """
    by_id = {f.id: f for f in findings}
    absorbed: set[str] = set()
    redirect: dict[str, str] = {}
    result: dict[str, Finding] = {}
    for group in groups:
        members = [by_id[i] for i in dict.fromkeys(group.finding_ids) if i in by_id]
        if group.keep not in by_id or len(members) < 2 or absorbed & {m.id for m in members}:
            continue
        keep = by_id[group.keep]
        evidence = list(keep.evidence)
        for m in members:
            for e in m.evidence:
                if len(evidence) < _MAX_EVIDENCE and all(
                        (e.block_id, match_key(e.quote)) != (x.block_id, match_key(x.quote))
                        for x in evidence):
                    evidence.append(e)
        merged = [*keep.merged]
        for m in members:
            if m.id == keep.id:
                continue
            merged += [MergedFinding(title=m.title, criterion=m.criterion, severity=m.severity,
                                     explanation=m.explanation), *m.merged]
        result[keep.id] = keep.model_copy(update={
            "merged": merged,
            "severity": min((m.severity for m in members), key=lambda s: s.rank),
            "unit_ids": sorted({u for m in members for u in m.unit_ids}),
            "evidence": evidence,
            "trail": [*keep.trail, *(f"merged:{m.id}" for m in members if m.id != keep.id)],
        })
        absorbed |= {m.id for m in members}
        redirect.update({m.id: keep.id for m in members})
    kept = [result.get(f.id, f) for f in findings if f.id not in absorbed or f.id in result]
    return kept, redirect


def renumber(findings: list[Finding]) -> tuple[list[Finding], dict[str, str]]:
    ordered = sorted(findings, key=lambda f: f.severity.rank)
    mapping = {f.id: f"D-{n:03d}" for n, f in enumerate(ordered, 1)}
    return [f.model_copy(update={"id": mapping[f.id]}) for f in ordered], mapping


def questions_from(drafts: list[tuple[str, list[str], str | None]],
                   findings: list[Finding]) -> list[Question]:
    by_id = {f.id: f for f in findings}
    built = []
    for text, ids, to in drafts:
        linked = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        if not linked or not text.strip():
            continue
        worst = min((f.severity for f in linked), key=lambda s: s.rank)
        built.append(Question(id="", finding_ids=[f.id for f in linked], text=text.strip(),
                              addressee=ADDRESSEE_RU.get(to or "", "аналитик"),
                              priority=_PRIORITY[worst],
                              blocks_development=worst == Severity.CRITICAL))
    built.sort(key=lambda q: q.priority)
    return [q.model_copy(update={"id": f"Q-{n:02d}"}) for n, q in enumerate(built, 1)]


def reviewer_questions(findings: list[Finding]) -> list[tuple[str, list[str], str | None]]:
    """Fallback: the reviewers' own questions, exact duplicates merged."""
    by_key: dict[str, tuple[str, list[str], str | None]] = {}
    for f in findings:
        if f.question:
            entry = by_key.setdefault(match_key(f.question), (f.question, [], f.question_to))
            entry[1].append(f.id)
    return list(by_key.values())


class QuestionsStage(Stage):
    code = "s8"
    title = "Сведение и вопросы"
    artifact = "s8_questions.json"

    def run(self, ctx: RunContext) -> Edited:
        state = ctx.state
        findings = state.findings
        groups: list[DuplicateGroup] = []
        drafts = reviewer_questions(findings)
        editor_used = False

        if findings:
            instructions, ids = prompt_store.compose("s8_editor", legend=LEGEND)
            prompt = (document_view(state.document, {b.id for b in state.document.blocks},
                                    ctx.settings.window_chars)
                      + f"\n\n<findings>\n{_listing(findings)}\n</findings>\n")
            result = ctx.llm.many([dict(step="s8_editor", role="editor",
                                        instructions=instructions, prompt=prompt,
                                        output_type=EditorAnswer, prompt_ids=ids,
                                        check=editor_check(findings))])[0]
            if isinstance(result, LLMError):
                state.check_failures.append(CheckFailure(
                    stage=self.code, cell="редактор отчёта",
                    message=result.message + " Дубли не сведены, вопросы взяты у рецензентов."))
            else:
                editor_used = True
                groups = result.duplicates
                drafts = [(q.text, q.finding_ids, q.question_to) for q in result.questions]

        merged, redirect = apply_groups(findings, groups)
        renumbered, mapping = renumber(merged)
        drafts = [(text, [mapping[redirect.get(i, i)] for i in ids
                          if redirect.get(i, i) in mapping], to)
                  for text, ids, to in drafts]
        # Safety net: a critical or major finding the editor left without a question keeps
        # the reviewer's own question.
        covered = {i for _, ids, _ in drafts for i in ids}
        drafts += [(text, ids, to) for text, ids, to in reviewer_questions(
            [f for f in renumbered if f.severity != Severity.MINOR and f.id not in covered])]
        questions = questions_from(drafts, renumbered)

        state.findings = renumbered
        state.questions = questions
        ctx.trace.event("editor_summary", stage=self.code, editor_used=editor_used,
                        findings_before=len(findings), findings_after=len(renumbered),
                        questions=len(questions))
        return Edited(findings=renumbered, questions=questions, merged_groups=groups,
                      editor_used=editor_used)
