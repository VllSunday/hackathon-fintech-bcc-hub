"""Data contracts shared by pipeline stages.

Draft v0. These models are the interface between the three work streams:
reading (produces `DocumentIR`), checks (produce `Unit`, `Finding`, `Question`)
and the report (consumes everything). Change them deliberately and together.

Free-text fields that end up in the report (`summary`, `title`, `explanation`,
`suggestion`, `text`) are written in Russian. Quotes are verbatim document text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from reqaudit.criteria import CRITERIA, Criterion, Level

# --- s0: intake -------------------------------------------------------------------------


class SourceFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    MARKDOWN = "markdown"
    TEXT = "text"
    HTML = "html"
    RTF = "rtf"
    DOC_LEGACY = "doc_legacy"
    ZIP_OTHER = "zip_other"
    BINARY = "binary"


class IntakeResult(BaseModel):
    path: str
    file_name: str
    extension: str
    size_bytes: int
    sha256: str
    detected_format: SourceFormat
    extension_mismatch: bool = False
    encoding: str | None = None
    text: str | None = Field(
        default=None, description="Decoded content, only for text-like formats."
    )
    warnings: list[str] = Field(default_factory=list)


# --- s1: reading ------------------------------------------------------------------------


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    CODE = "code"
    CAPTION = "caption"
    HEADER_FOOTER = "header_footer"
    IMAGE_TEXT = "image_text"


class BlockSource(StrEnum):
    PLAIN_TEXT = "plain_text"
    TEXT_LAYER = "text_layer"
    STRUCTURE = "structure"
    VLM = "vlm"
    OCR = "ocr"


class Anchor(BaseModel):
    """Where a block lives in the original document. Every quote in the report resolves here."""

    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    paragraph: int | None = Field(default=None, description="Paragraph number in formats "
                                  "without pages or lines, e.g. DOCX.")
    section_path: list[str] = Field(default_factory=list)


class Block(BaseModel):
    id: str = Field(description="Stable id within one run, e.g. 'b0042'.")
    kind: BlockKind
    text: str
    anchor: Anchor
    source: BlockSource
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    hidden: bool = Field(default=False, description="Invisible in the rendered document.")
    table_header: list[str] | None = None

    @property
    def doubtful(self) -> bool:
        """Read from an image, and two readings of the scan disagree here."""
        return self.source == BlockSource.VLM and self.confidence <= DOUBT_CONFIDENCE


# Confidence of scan text where two independent readings differ (reading/vision.py).
DOUBT_CONFIDENCE = 0.6


class ReadIssue(BaseModel):
    """A defect of the file itself found while reading: broken markup, hidden text, bad encoding.

    Issues are evidence for `document_integrity` and `suspicious_content` findings later.
    """

    kind: str
    message: str
    line: int | None = None
    block_id: str | None = None


class DocumentIR(BaseModel):
    title: str | None = None
    format: SourceFormat
    blocks: list[Block]
    line_count: int | None = None
    reader_chain: list[str] = Field(default_factory=list)
    issues: list[ReadIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --- s2: profile and units --------------------------------------------------------------


class PartRole(StrEnum):
    MAIN = "main"
    CHILD_SPEC = "child_spec"
    APPENDIX = "appendix"
    OTHER = "other"


class DocumentPart(BaseModel):
    name: str = Field(description="The part's name as written in its heading.")
    role: PartRole = Field(description="main: the specification itself; child_spec: a "
                           "detailed specification of something the main part refers to; "
                           "appendix: supporting material; other.")
    start_block_id: str = Field(description="Id of the block where the part starts, "
                                "usually its heading.")
    parent: str | None = Field(default=None, description="For child_spec: the name of the "
                               "part it details. Otherwise null.")


MAX_FOCUS_CHECKS = 5


class FocusCheck(BaseModel):
    """A check this document needs beyond the standard ones, proposed by the profiler."""

    title: str = Field(description="In Russian, a few words: what is checked, e.g. "
                       "'Жизненный цикл статусов заявки'.")
    instruction: str = Field(description="What to verify and against what, in two to four "
                             "sentences, naming the sections and the cross-checks.")
    criteria: list[str] = Field(description="Criterion codes from the list given.")
    start_block_ids: list[str] = Field(default_factory=list, description="Ids of the "
                                       "headings or first blocks of the sections involved.")


class DocumentProfile(BaseModel):
    """What the document is and what it contains. Decides which checks apply (s4)."""

    summary: str = Field(description="One or two sentences in Russian: what the document "
                         "specifies, for which product.")
    parts: list[DocumentPart] = Field(min_length=1, description="Parts in document order.")
    numbering: str = Field(description="In Russian: how the document identifies its "
                           "requirements, e.g. 'Ф-1…Ф-4 для функциональных требований', or "
                           "'нумерации нет'.")
    has_acceptance_criteria: bool
    has_api_spec: bool
    has_data_requirements: bool
    has_non_functional: bool
    has_ui_spec: bool
    focus_checks: list[FocusCheck] = Field(
        default_factory=list, description="Checks specific to this document's content, "
        f"beyond the standard ones; at most {MAX_FOCUS_CHECKS}, often none.")

    @field_validator("focus_checks", mode="after")
    @classmethod
    def _at_most_five(cls, value: list[FocusCheck]) -> list[FocusCheck]:
        return value[:MAX_FOCUS_CHECKS]


class SkipReason(StrEnum):
    GOAL = "goal"
    CONTEXT = "context"
    METADATA = "metadata"
    TEMPLATE = "template"
    EXAMPLE = "example"
    APPROVAL = "approval"
    EMPTY = "empty"
    OTHER = "other"


class SkippedBlock(BaseModel):
    block_id: str
    reason: SkipReason

    @field_validator("reason", mode="before")
    @classmethod
    def _unknown_reason_is_other(cls, value: object) -> object:
        # A model inventing a reason should not cost the whole answer.
        known = {r.value for r in SkipReason}
        return value if value in known else SkipReason.OTHER.value


class Coverage(BaseModel):
    """How every visible non-heading block was accounted for. The assumption sensor of s2."""

    blocks_checked: int
    in_units: int
    skipped: int
    unclassified: list[str] = Field(default_factory=list)
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    dropped_units: list[str] = Field(default_factory=list,
                                     description="Why model units were rejected by code.")
    obligations_outside: dict[str, str] = Field(
        default_factory=dict, description="Second segmenter: blocks outside every unit that "
        "state an obligation explicitly, with the word found; skips the model justified "
        "as goal, example, template or approval are not listed.")


class UnitKind(StrEnum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    DATA = "data"
    INTEGRATION = "integration"
    UI = "ui"
    ACCEPTANCE_CRITERION = "acceptance_criterion"
    API_CONTRACT = "api_contract"
    CONSTRAINT = "constraint"
    OTHER = "other"


class Unit(BaseModel):
    """One requirement-like statement the agent will check."""

    id: str = Field(description="Report id, e.g. 'R-001'.")
    doc_label: str | None = Field(default=None, description="Id in the document, e.g. 'Ф-1'.")
    kind: UnitKind
    part: str | None = Field(default=None, description="Document part, e.g. parent or child spec.")
    block_ids: list[str]
    quote: str
    summary: str


class Segmentation(BaseModel):
    """Artifact of s2."""

    profile: DocumentProfile
    units: list[Unit]
    skipped: list[SkippedBlock]
    coverage: Coverage


# --- s3: facts --------------------------------------------------------------------------


class Fact(BaseModel):
    id: str = Field(description="Report id, e.g. 'F-001'.")
    entity: str
    entity_label: str
    attribute: str
    value: str
    normalized: str = Field(description="Value after code normalisation, used for comparing.")
    condition: str | None = None
    block_id: str
    quote: str
    dimension: str | None = Field(default=None, description="For unit facts: what the unit "
                                  "measures (money, time, data_size...), as the model named it.")


class FactConflict(BaseModel):
    """A discrepancy code found between facts. A candidate only: a model judges it in s5."""

    kind: str = Field(description="different_values or unit_mismatch")
    entity: str
    attribute: str
    fact_ids: list[str]
    summary: str = Field(description="Russian, one line, what differs.")


class FactTable(BaseModel):
    """Artifact of s3."""

    facts: list[Fact]
    conflicts: list[FactConflict]
    dropped: int = 0


# --- s4: cells --------------------------------------------------------------------------


class CellKind(StrEnum):
    REQUIREMENTS = "requirements"
    CONSISTENCY = "consistency"
    PARENT_CHILD = "parent_child"
    ACCEPTANCE = "acceptance"
    COMPLETENESS = "completeness"
    API_CONTRACT = "api_contract"
    VALUES = "values"
    GENERAL = "general"
    FOCUS = "focus"


class Cell(BaseModel):
    """One unit of checking work: what to look at and against which criteria."""

    id: str
    kind: CellKind
    title: str
    unit_ids: list[str] = Field(default_factory=list,
                                description="Requirements under check; empty for set cells.")
    criteria: list[Criterion]
    instruction: str = Field(default="", description="Focus cells: what to verify, as the "
                             "profiler wrote it for this document.")
    block_ids: list[str] = Field(default_factory=list, description="Focus cells: where the "
                                 "sections involved start.")


class CellPlan(BaseModel):
    """Artifact of s4."""

    cells: list[Cell]
    notes: list[str] = Field(default_factory=list)


# --- s5–s7: findings --------------------------------------------------------------------


class Scope(StrEnum):
    REQUIREMENT = "requirement"
    PAIR = "pair"
    SET = "set"
    DOCUMENT = "document"


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"

    @property
    def rank(self) -> int:
        """0 for the most severe: sort findings by it."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.MAJOR: 1, Severity.MINOR: 2}


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class Evidence(BaseModel):
    block_id: str
    quote: str
    verified: bool = Field(default=False, description="Set only by the evidence gate (s7).")


# What the agent decided about a finding after the author answered its question (s8b).
AnswerDecision = Literal["confirmed", "resolved", "changed"]


class Finding(BaseModel):
    id: str = Field(description="Report id, e.g. 'D-001'.")
    scope: Scope
    criterion: Criterion
    unit_ids: list[str] = Field(default_factory=list)
    title: str
    explanation: str
    evidence: list[Evidence]
    severity: Severity
    suggestion: str | None = None
    status: FindingStatus = FindingStatus.CANDIDATE
    trail: list[str] = Field(
        default_factory=list, description="Roles that touched the finding, in order."
    )
    question: str | None = None
    question_to: str | None = None
    cell: str | None = None
    review_note: str | None = Field(default=None, description="Why the opponent, the arbiter "
                                    "or a gate kept, changed or rejected the finding.")
    author_answer: str | None = None
    answer_decision: AnswerDecision | None = Field(
        default=None, description="Decision after the author's answer (s8b).")
    answer_note: str | None = None
    document_fix: str | None = Field(default=None, description="Wording to put into the "
                                     "document, following the author's answer.")

    @property
    def resolved(self) -> bool:
        """The author's answer showed the document covers it (s8b)."""
        return self.answer_decision == "resolved"

    @property
    def in_report(self) -> bool:
        """Counted as a defect: confirmed by review and not resolved by the author's answer.
        The report and the web UI use it through report.views; the eval stand applies the
        same rule to saved JSON."""
        return self.status == FindingStatus.CONFIRMED and not self.resolved


class CheckFailure(BaseModel):
    """A cell or review batch the model could not do; its scope stays unchecked."""

    stage: str
    cell: str
    message: str


class MarkerRecord(BaseModel):
    """A marker word found by code in s5 and the reviewer's decision on it."""

    id: str
    unit_id: str
    category: str
    word: str
    defect: bool | None = Field(default=None, description="None when the cell was not done.")
    reason: str | None = None


# --- s8: questions ----------------------------------------------------------------------


class Question(BaseModel):
    id: str = Field(description="Report id, e.g. 'Q-01'.")
    finding_ids: list[str]
    addressee: str = Field(description="Role, e.g. 'аналитик', 'владелец продукта'.")
    text: str
    priority: int = Field(ge=1, le=3, description="1 is the most urgent.")
    blocks_development: bool = False
    answer: str | None = Field(default=None, description="The author's answer, if given.")


def scope_of(criterion: Criterion, unit_ids: list[str]) -> Scope:
    """What a finding is about: one requirement, a pair, the set, or the document."""
    level = CRITERIA[criterion].level
    if level == Level.REQUIREMENT:
        return Scope.REQUIREMENT
    if level == Level.DOCUMENT:
        return Scope.DOCUMENT
    return Scope.PAIR if criterion == Criterion.SET_CONSISTENT and len(unit_ids) == 2 \
        else Scope.SET


# --- run state --------------------------------------------------------------------------


class StageError(BaseModel):
    stage: str
    kind: str
    message: str


class RunState(BaseModel):
    """Everything the stages have produced so far. Serialised to runs/<id>/state.json."""

    intake: IntakeResult | None = None
    document: DocumentIR | None = None
    profile: DocumentProfile | None = None
    units: list[Unit] = Field(default_factory=list)
    coverage: Coverage | None = None
    facts: FactTable | None = None
    cells: list[Cell] = Field(default_factory=list)
    plan_notes: list[str] = Field(default_factory=list)
    candidates: list[Finding] = Field(default_factory=list)
    markers: list[MarkerRecord] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    rejected: list[Finding] = Field(default_factory=list)
    check_failures: list[CheckFailure] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    errors: list[StageError] = Field(default_factory=list)
