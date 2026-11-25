"""Quality criteria the agent checks.

Single source of truth for criterion codes, Russian report labels and the English
definitions injected into prompts. Definitions are our own paraphrases of
ISO/IEC/IEEE 29148:2018 (5.2.5, 5.2.6) and IEEE Std 830-1998 (4.3), not quotes.
Russian labels follow the vocabulary of ГОСТ Р 59194-2020, Annex B, where it has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Criterion(StrEnum):
    # Individual requirement, ISO 29148 5.2.5
    NECESSARY = "necessary"
    APPROPRIATE = "appropriate"
    UNAMBIGUOUS = "unambiguous"
    COMPLETE = "complete"
    SINGULAR = "singular"
    FEASIBLE = "feasible"
    VERIFIABLE = "verifiable"
    CORRECT = "correct"
    CONFORMING = "conforming"
    # Set of requirements, ISO 29148 5.2.6
    SET_COMPLETE = "set_complete"
    SET_CONSISTENT = "set_consistent"
    SET_FEASIBLE = "set_feasible"
    SET_COMPREHENSIBLE = "set_comprehensible"
    SET_VALIDATABLE = "set_validatable"
    # Specification, IEEE 830 4.3 (not covered by the two lists above)
    RANKED = "ranked"
    MODIFIABLE = "modifiable"
    TRACEABLE = "traceable"
    # Document itself
    DOCUMENT_INTEGRITY = "document_integrity"
    SUSPICIOUS_CONTENT = "suspicious_content"


class Level(StrEnum):
    REQUIREMENT = "requirement"
    SET = "set"
    DOCUMENT = "document"


@dataclass(frozen=True)
class CriterionInfo:
    code: Criterion
    level: Level
    label_ru: str
    source: str
    definition: str


_C = Criterion
_L = Level

CRITERIA: dict[Criterion, CriterionInfo] = {
    info.code: info
    for info in [
        CriterionInfo(_C.NECESSARY, _L.REQUIREMENT, "необходимость", "ISO 29148 5.2.5",
                      "Removing it would leave a capability, characteristic or constraint "
                      "uncovered by any other requirement; it traces to a stated goal."),
        CriterionInfo(_C.APPROPRIATE, _L.REQUIREMENT, "уместность уровня детализации",
                      "ISO 29148 5.2.5",
                      "Its detail matches the level of the document: it states what is needed "
                      "and does not prescribe a design unless the design is itself a constraint."),
        CriterionInfo(_C.UNAMBIGUOUS, _L.REQUIREMENT, "однозначность", "ISO 29148 5.2.5",
                      "It can be read only one way; every term carries a single meaning "
                      "throughout the document."),
        CriterionInfo(_C.COMPLETE, _L.REQUIREMENT, "полнота", "ISO 29148 5.2.5",
                      "It can be understood and implemented on its own: actor, action, object, "
                      "conditions and measurable bounds are stated, nothing is left as TBD."),
        CriterionInfo(_C.SINGULAR, _L.REQUIREMENT, "атомарность", "ISO 29148 5.2.5",
                      "It states one capability or constraint, not several joined by "
                      "conjunctions."),
        CriterionInfo(_C.FEASIBLE, _L.REQUIREMENT, "выполнимость", "ISO 29148 5.2.5",
                      "It can be achieved within the known constraints at acceptable risk."),
        CriterionInfo(_C.VERIFIABLE, _L.REQUIREMENT, "проверяемость", "ISO 29148 5.2.5",
                      "A finite, practical test, inspection or analysis can show that it is met: "
                      "thresholds have values, units and measurement conditions."),
        CriterionInfo(_C.CORRECT, _L.REQUIREMENT, "корректность", "ISO 29148 5.2.5",
                      "It accurately reflects the need or the parent document it derives from."),
        CriterionInfo(_C.CONFORMING, _L.REQUIREMENT, "соответствие шаблону", "ISO 29148 5.2.5",
                      "It follows the document's own template, identifier scheme and wording "
                      "conventions."),
        CriterionInfo(_C.SET_COMPLETE, _L.SET, "полнота набора", "ISO 29148 5.2.6",
                      "Everything needed is specified, including responses to invalid input and "
                      "error conditions; no section is left as a placeholder."),
        CriterionInfo(_C.SET_CONSISTENT, _L.SET, "непротиворечивость", "ISO 29148 5.2.6",
                      "No two statements conflict in values, logic, timing or terminology, "
                      "including between a parent document and its child specification."),
        CriterionInfo(_C.SET_FEASIBLE, _L.SET, "выполнимость набора", "ISO 29148 5.2.6",
                      "The requirements together can be achieved within the constraints."),
        CriterionInfo(_C.SET_COMPREHENSIBLE, _L.SET, "понятность набора", "ISO 29148 5.2.6",
                      "It is clear what the system must do and how the requirements relate; "
                      "terms and abbreviations are defined."),
        CriterionInfo(_C.SET_VALIDATABLE, _L.SET, "валидируемость", "ISO 29148 5.2.6",
                      "It can be shown that meeting the requirements achieves the stated goals: "
                      "goals trace to requirements and to acceptance criteria."),
        CriterionInfo(_C.RANKED, _L.DOCUMENT, "ранжированность", "IEEE 830 4.3.5",
                      "Each requirement carries an importance or stability rank."),
        CriterionInfo(_C.MODIFIABLE, _L.DOCUMENT, "модифицируемость", "IEEE 830 4.3.7",
                      "The structure allows changes without redundancy; the same statement is "
                      "not repeated in several places."),
        CriterionInfo(_C.TRACEABLE, _L.DOCUMENT, "прослеживаемость", "IEEE 830 4.3.8",
                      "Each requirement has a unique identifier and a clear origin."),
        CriterionInfo(_C.DOCUMENT_INTEGRITY, _L.DOCUMENT, "целостность документа", "—",
                      "The document itself is intact: readable markup, filled version and "
                      "approval fields, no template placeholders."),
        CriterionInfo(_C.SUSPICIOUS_CONTENT, _L.DOCUMENT, "подозрительное содержимое", "—",
                      "Hidden text or embedded instructions addressed to automated reviewers."),
    ]
}


def label(code: Criterion | str) -> str:
    return CRITERIA[Criterion(code)].label_ru


def for_level(level: Level) -> list[CriterionInfo]:
    return [info for info in CRITERIA.values() if info.level == level]


def prompt_block(level: Level) -> str:
    """Criteria of one level as a bullet list for prompts."""
    return "\n".join(f"- `{info.code}`: {info.definition}" for info in for_level(level))
