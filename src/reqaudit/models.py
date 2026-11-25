"""Data contracts shared by pipeline stages.

Draft v0. These models are the interface between the three work streams:
reading (produces `DocumentIR`), checks (produce `Unit`, `Finding`, `Question`)
and the report (consumes everything). Change them deliberately and together.

Free-text fields that end up in the report (`summary`, `title`, `explanation`,
`suggestion`, `text`) are written in Russian. Quotes are verbatim document text.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from reqaudit.criteria import Criterion

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


class DocumentIR(BaseModel):
    title: str | None = None
    format: SourceFormat
    blocks: list[Block]
    reader_chain: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --- s2: units --------------------------------------------------------------------------


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


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class Evidence(BaseModel):
    block_id: str
    quote: str
    verified: bool = Field(default=False, description="Set only by the evidence gate (s7).")


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


# --- s8: questions ----------------------------------------------------------------------


class Question(BaseModel):
    id: str = Field(description="Report id, e.g. 'Q-01'.")
    finding_ids: list[str]
    addressee: str = Field(description="Role, e.g. 'аналитик', 'владелец продукта'.")
    text: str
    priority: int = Field(ge=1, le=3, description="1 is the most urgent.")
    blocks_development: bool = False
