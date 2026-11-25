"""s3 Facts: the model writes out checkable facts, code lines them up and finds disagreements.

Division of labour:
- the model extracts facts with keys: one key per real-world thing across the whole
  document, a fixed list of attributes, a verbatim quote for each fact;
- code checks every quote, normalises values (₸ / тенге / KZT are one unit, integer /
  целое число are one type) and computes conflict candidates:
  * one entity with different values of one attribute;
  * two quantities compared with each other in different units, following
    `derived_from` links, e.g. a monthly total of purchases in tiyn against a
    threshold in tenge;
- a reviewer in s5 ("Сверка значений") judges every candidate. Code never reports a
  defect on its own here: a different value under a different condition can be fine.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, field_validator

from reqaudit import prompt_store
from reqaudit.llm.checks import Check, combine
from reqaudit.llm.client import LLMError
from reqaudit.models import Block, CheckFailure, Fact, FactConflict, FactTable
from reqaudit.reading.render import LEGEND, render_blocks, split_windows, visible
from reqaudit.stages.base import RunContext, Stage
from reqaudit.stages.review_common import EvidenceDraft, evidence_problem, verify_evidence

ROLE = "analyst"
Attribute = Literal["value", "unit", "type", "range", "default", "required", "format", "method",
                    "status", "timing", "period", "timezone", "count", "rule", "compared_with",
                    "derived_from"]
_ATTRIBUTES = set(Attribute.__args__)
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
_RELATIONS = {"compared_with", "derived_from"}
_COMPARABLE = {"value", "unit", "type", "range", "default", "required", "format", "method",
               "timing", "period", "timezone", "count"}

# Value normalisation: synonyms that mean the same unit or type.
_SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(₸|kzt|тг\.?|тенге\w*)$"), "kzt"),
    (re.compile(r"^(тиын\w*|tiyn\w*)$"), "tiyn"),
    (re.compile(r"^(int|integer|int32|int64|цел(ое|ый|ым|ого|ые)( числ\w*)?( тенге)?)$"),
     "integer"),
    (re.compile(r"^(сек\.?|секунд\w*|seconds?|s)$"), "second"),
    (re.compile(r"^(мин\.?|минут\w*|minutes?|min)$"), "minute"),
    (re.compile(r"^(час\w*|hours?|h)$"), "hour"),
    (re.compile(r"^(руб\w*|rub|₽)$"), "rub"),
    (re.compile(r"^(usd|\$|доллар\w*)$"), "usd"),
    (re.compile(r"^(str|string|строка|строковый|text)$"), "string"),
    (re.compile(r"^(bool|boolean|логический)$"), "boolean"),
    (re.compile(r"^(да|yes|true|обязательн\w*|mandatory|required)$"), "yes"),
    (re.compile(r"^(нет|no|false|необязательн\w*|optional)$"), "no"),
    (re.compile(r"^(выключено|выключен\w*|off|disabled|отключен\w*)$"), "off"),
    (re.compile(r"^(включено|включен\w*|on|enabled)$"), "on"),
]


class FactDraft(BaseModel):
    entity: str
    entity_label: str
    attribute: Attribute | str
    value: str
    condition: str | None = None
    block_id: str
    quote: str

    @field_validator("entity", mode="before")
    @classmethod
    def _snake(cls, value: object) -> object:
        return re.sub(r"[^a-z0-9_]+", "_", str(value).strip().lower()).strip("_") or "unknown"


class FactsAnswer(BaseModel):
    facts: list[FactDraft]


def normalize(value: str) -> str:
    v = " ".join(value.lower().replace("ё", "е").split())
    v = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", v)  # 1 000 000 -> 1000000
    v = re.sub(r"[‐-―−]", "-", v)
    v = v.strip(" .,;«»\"'`")
    for pattern, canonical in _SYNONYMS:
        if pattern.match(v):
            return canonical
    return v


def facts_check(blocks: dict[str, Block], known: set[str]) -> Check:
    def check(answer: FactsAnswer) -> str | None:
        complaints = []
        keys = known | {f.entity for f in answer.facts}
        for n, f in enumerate(answer.facts):
            where = f"facts[{n}] ({f.entity}.{f.attribute})"
            if f.attribute not in _ATTRIBUTES:
                complaints.append(f"{where}: attribute must be one of {sorted(_ATTRIBUTES)}")
            if not _KEY.match(f.entity):
                complaints.append(f"{where}: entity must be an English snake_case key")
            if f.attribute in _RELATIONS and f.value not in keys:
                complaints.append(f"{where}: value must be the key of another entity you "
                                  f"listed, got {f.value!r}")
            if problem := evidence_problem(EvidenceDraft(block_id=f.block_id, quote=f.quote),
                                           blocks):
                complaints.append(f"{where}: {problem}")
        if not complaints:
            return None
        return ("Fix these problems and return the full answer again:\n"
                + "\n".join(f"- {c}" for c in complaints[:25]))

    return combine(check)


def _units(facts: list[Fact]) -> dict[str, set[str]]:
    """Units per entity, inherited along `derived_from` links when the entity has none."""
    own: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        if f.attribute == "unit":
            own[f.entity].add(f.normalized)
        elif f.attribute == "derived_from":
            parents[f.entity].add(f.value)

    def resolve(entity: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if own.get(entity):
            return own[entity]
        found: set[str] = set()
        for parent in parents.get(entity, ()):
            if parent not in seen:
                found |= resolve(parent, seen | {entity})
        return found

    return {e: resolve(e) for e in {f.entity for f in facts} | set(parents)}


def _lineage(entity: str, facts: list[Fact]) -> set[str]:
    """The entity and everything it is derived from, directly or through other entities."""
    parents: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        if f.attribute == "derived_from":
            parents[f.entity].add(f.value)
    found, todo = {entity}, [entity]
    while todo:
        for parent in parents.get(todo.pop(), ()):
            if parent not in found:
                found.add(parent)
                todo.append(parent)
    return found


_DIMENSION = {"kzt": "деньги", "tiyn": "деньги", "rub": "деньги", "usd": "деньги",
              "second": "время", "minute": "время", "hour": "время"}


def _label_key(label: str) -> str:
    return re.sub(r"[^\wё]+", " ", label.lower()).strip()


def find_conflicts(facts: list[Fact], table_of: dict[str, str] | None = None
                   ) -> list[FactConflict]:
    """Discrepancies code can see without understanding the text.

    Facts are grouped twice, by the model's entity key and by the name the document uses,
    because a model does not always give one thing one key. `table_of` maps block ids to a
    table identity; values that differ only between rows of one table are examples.
    """
    table_of = table_of or {}
    conflicts: list[FactConflict] = []
    groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for f in facts:
        if f.attribute in _COMPARABLE:
            groups[("entity:" + f.entity, f.attribute)].append(f)
            groups[("label:" + _label_key(f.entity_label), f.attribute)].append(f)
    seen: set[frozenset[str]] = set()
    for (_key, attribute), group in sorted(groups.items()):
        ids = frozenset(f.id for f in group)
        values = {f.normalized for f in group}
        conditions = [f.condition for f in group]
        # Different values under different stated conditions are expected, not a conflict.
        situational = all(conditions) and len(set(conditions)) == len(conditions)
        tables = {table_of.get(f.block_id) for f in group}
        # Rows of one table with different *values* are examples (acceptance criteria, sample
        # cases). Different types, units or ranges in one table are still a conflict.
        one_table = attribute == "value" and len(tables) == 1 and None not in tables
        if len(values) < 2 or situational or one_table or ids in seen:
            continue
        seen.add(ids)
        shown = "; ".join(f"«{f.value}» ({f.id})" for f in group)
        conflicts.append(FactConflict(
            kind="different_values", entity=group[0].entity, attribute=attribute,
            fact_ids=[f.id for f in group],
            summary=f"{group[0].entity_label}: разные значения «{attribute}»: {shown}"))

    # Mixed units in one dimension anywhere in the document: e.g. tenge and tiyn, without
    # relying on how the model linked the quantities.
    by_dimension: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        if f.attribute == "unit" and f.normalized in _DIMENSION:
            by_dimension[_DIMENSION[f.normalized]].append(f)
    for dimension, group in sorted(by_dimension.items()):
        units = sorted({f.normalized for f in group})
        if len(units) > 1:
            shown = "; ".join(f"{f.entity_label} — «{f.value}» ({f.id})" for f in group)
            conflicts.append(FactConflict(
                kind="mixed_units", entity=group[0].entity, attribute="unit",
                fact_ids=[f.id for f in group],
                summary=f"В документе разные единицы измерения ({dimension}: "
                        f"{', '.join(units)}): {shown}"))

    units = _units(facts)
    labels = {f.entity: f.entity_label for f in facts}
    seen_pairs: set[frozenset[str]] = set()
    for f in facts:
        if f.attribute != "compared_with":
            continue
        a, b = f.entity, f.value
        pair = frozenset((a, b))
        ua, ub = units.get(a, set()), units.get(b, set())
        if pair in seen_pairs or not ua or not ub or ua & ub:
            continue
        seen_pairs.add(pair)
        # Unit facts along both lineages, so the reviewer sees where each unit comes from.
        related = _lineage(a, facts) | _lineage(b, facts)
        derivations = [x.id for x in facts
                       if x.attribute == "derived_from" and x.entity in related]
        unit_facts = [x.id for x in facts if x.attribute == "unit" and x.entity in related]
        conflicts.append(FactConflict(
            kind="unit_mismatch", entity=a, attribute="unit",
            fact_ids=[f.id, *derivations, *unit_facts],
            summary=(f"{labels.get(a, a)} ({', '.join(sorted(ua))}) сравнивается с "
                     f"{labels.get(b, b)} ({', '.join(sorted(ub))}): единицы различаются")))
    return conflicts


def tables_of(document) -> dict[str, str]:
    """Block id -> identity of the table it belongs to (rows of one table share it)."""
    out: dict[str, str] = {}
    current: str | None = None
    for block in document.blocks:
        if block.kind.value == "table":
            current = block.id
            out[block.id] = current
        elif block.kind.value == "table_row" and current is not None:
            out[block.id] = current
        else:
            current = None
    return out


def fact_table_view(table: FactTable) -> str:
    """Facts grouped by entity, then the conflict candidates, for the values reviewer."""
    by_entity: dict[str, list[Fact]] = defaultdict(list)
    for f in table.facts:
        by_entity[f.entity].append(f)
    lines = []
    for entity, facts in by_entity.items():
        lines.append(f"{entity} — {facts[0].entity_label}")
        for f in facts:
            cond = f" [условие: {f.condition}]" if f.condition else ""
            lines.append(f"  {f.id} {f.attribute} = {f.value}{cond} · [{f.block_id}] «{f.quote}»")
    conflicts = "\n".join(f"- {c.kind}: {c.summary} (facts {', '.join(c.fact_ids)})"
                          for c in table.conflicts) or "- none"
    return ("<fact_table>\n" + "\n".join(lines) + "\n</fact_table>\n\n"
            f"<code_found_discrepancies>\n{conflicts}\n</code_found_discrepancies>")


class FactsStage(Stage):
    code = "s3"
    title = "Факты"
    artifact = "s3_facts.json"

    def run(self, ctx: RunContext) -> FactTable:
        document = ctx.state.document
        assert document is not None, "s3 needs s1"
        blocks = {b.id: b for b in visible(document.blocks)}
        windows = split_windows(document.blocks, ctx.settings.window_chars)

        drafts: list[FactDraft] = []
        known: set[str] = set()
        for k, window in enumerate(windows, 1):
            listed = (f"\nEntity keys already used in earlier parts of this document; reuse them "
                      f"for the same things: {', '.join(sorted(known))}.\n") if known else ""
            instructions, ids = prompt_store.compose("s3_facts", legend=LEGEND,
                                                     known_entities=listed)
            scope = "" if len(windows) == 1 else f"Part {k} of {len(windows)}. "
            try:
                answer = ctx.llm.structured(
                    step=f"s3_facts[{k}/{len(windows)}]", role=ROLE, instructions=instructions,
                    prompt=f"{scope}<document>\n{render_blocks(window)}\n</document>\n",
                    output_type=FactsAnswer, prompt_ids=ids, check=facts_check(blocks, known))
            except LLMError as err:
                # Facts are an extra check: without them the other checks still run, and the
                # report lists the value check as not done.
                ctx.state.check_failures.append(CheckFailure(
                    stage=self.code, cell="Таблица фактов и сверка значений",
                    message=err.message))
                ctx.state.facts = None
                return FactTable(facts=[], conflicts=[])
            drafts.extend(answer.facts)
            known |= {f.entity for f in answer.facts}

        facts: list[Fact] = []
        dropped = 0
        for d in drafts:
            evidence = verify_evidence(EvidenceDraft(block_id=d.block_id, quote=d.quote), blocks)
            if evidence is None or d.attribute not in _ATTRIBUTES:
                dropped += 1
                continue
            facts.append(Fact(id=f"F-{len(facts) + 1:03d}", entity=d.entity,
                              entity_label=d.entity_label.strip(), attribute=d.attribute,
                              value=d.value.strip(),
                              normalized=(d.value.strip() if d.attribute in _RELATIONS
                                          else normalize(d.value)),
                              condition=(d.condition or "").strip() or None,
                              block_id=evidence.block_id, quote=evidence.quote))

        table = FactTable(facts=facts, conflicts=find_conflicts(facts, tables_of(document)),
                          dropped=dropped)
        ctx.state.facts = table
        ctx.trace.event("facts_summary", stage=self.code, facts=len(facts), dropped=dropped,
                        conflicts=len(table.conflicts))
        return table
