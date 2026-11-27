"""Unit check by code: two money amounts the document compares must be in one unit.

The fact table (s3) records which quantities are compared («расходы достигают порога») and
in which unit each is stated or stored. When one side is in tenge and the other in tiyn,
and the document nowhere says how one is converted to the other, a sum 100 times off is
one careless comparison away. Code already lists this as a discrepancy for the reviewer of
the value check; on the organisers' document and on N2 the reviewer waved it through («the
API says tenge») although the stored amount was in tiyn. The units of two compared amounts
are a fact of the text, so here code files the finding itself, as it does for field types
(contract.py).

Kept narrow on purpose:
- money only. «30 секунд» against «1 минута» is ordinary prose, not a defect;
- only units code knows (UNIT_DIMENSION), never a unit the model named;
- only quantities the fact table links with `compared_with`;
- not when the document mentions a conversion anywhere: then it is a judgement whether the
  rule covers this pair, and the judgement stays with the reviewer.
"""

from __future__ import annotations

import re

from reqaudit.criteria import Criterion
from reqaudit.models import (
    Block,
    Evidence,
    Fact,
    FactTable,
    Finding,
    FindingStatus,
    Severity,
    Unit,
    scope_of,
)
from reqaudit.stages.s3_facts import UNIT_DIMENSION, unit_sources

UNIT_RU = {"kzt": "тенге", "tiyn": "тиынах", "rub": "рублях", "usd": "долларах"}
# Any of these in the visible text means the document speaks about conversion.
_CONVERSION = re.compile(
    r"пересч[её]т|пересчит|конверт|привод\w+ к (?:тиын|тенге|копе|рубл|цент)|"
    r"умнож\w+ на|дел\w+ на 100|[×x*/]\s*100\b|100 (?:тиын|копе|цент)", re.IGNORECASE)


def _money(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if UNIT_DIMENSION.get(f.normalized) == "money"]


def mismatched_money(table: FactTable) -> list[tuple[Fact, Fact, Fact]]:
    """(comparison, unit of one side, unit of the other) for every compared pair of money
    amounts whose units differ; each pair once, in the order of the fact table."""
    found: list[tuple[Fact, Fact, Fact]] = []
    seen: set[frozenset[str]] = set()
    for f in table.facts:
        if f.attribute != "compared_with" or frozenset((f.entity, f.value)) in seen:
            continue
        left, right = _money(unit_sources(f.entity, table.facts)), \
            _money(unit_sources(f.value, table.facts))
        if not left or not right or {x.normalized for x in left} & {y.normalized for y in right}:
            continue  # a unit both sides share means they can be compared as stated
        seen.add(frozenset((f.entity, f.value)))
        found.append((f, left[0], right[0]))
    return found


def _finding(number: int, comparison: Fact, a: Fact, b: Fact, units: list[Unit]) -> Finding:
    evidence: list[Evidence] = []
    for fact in (a, b, comparison):
        if (fact.block_id, fact.quote) not in {(e.block_id, e.quote) for e in evidence}:
            evidence.append(Evidence(block_id=fact.block_id, quote=fact.quote, verified=True))
    blocks = {e.block_id for e in evidence}
    unit_ids = sorted(u.id for u in units if blocks & set(u.block_ids))
    unit_a, unit_b = UNIT_RU.get(a.normalized, a.value), UNIT_RU.get(b.normalized, b.value)
    return Finding(
        id=f"units#{number}", scope=scope_of(Criterion.SET_CONSISTENT, unit_ids),
        criterion=Criterion.SET_CONSISTENT, unit_ids=unit_ids,
        title=f"«{a.entity_label}» в {unit_a}, «{b.entity_label}» в {unit_b}: "
              "сравниваются без правила пересчёта",
        explanation=(
            f"Документ сравнивает две денежные величины, заданные в разных единицах: "
            f"«{a.entity_label}» — в {unit_a}, «{b.entity_label}» — в {unit_b}. Правила "
            "пересчёта одной единицы в другую в документе нет. Если сравнить значения как "
            "записано, результат разойдётся в 100 раз: условие сработает на сотой доле суммы "
            "или не сработает никогда. Расхождение найдено сверкой таблицы фактов кодом."),
        evidence=evidence,
        severity=Severity.MAJOR, status=FindingStatus.CONFIRMED, trail=["code:units"],
        suggestion=("Указать одну единицу для обеих величин либо прямо написать, в какой "
                    "единице выполняется сравнение и как пересчитывается вторая величина "
                    "(например: «сумма покупок в тиынах делится на 100 перед сравнением с "
                    "порогом в тенге»)."),
        question=(f"В какой единице сравниваются «{a.entity_label}» и «{b.entity_label}» — в "
                  f"{unit_a} или в {unit_b}, и где выполняется пересчёт?"),
        question_to="analyst",
    )


def unit_findings(table: FactTable | None, blocks: list[Block],
                  units: list[Unit]) -> list[Finding]:
    """Confirmed findings for compared money amounts in different units. `blocks` are the
    visible blocks: a conversion mentioned anywhere in them leaves the case to the reviewer."""
    if table is None or any(_CONVERSION.search(b.text) for b in blocks):
        return []
    return [_finding(n, comparison, a, b, units)
            for n, (comparison, a, b) in enumerate(mismatched_money(table), 1)]
