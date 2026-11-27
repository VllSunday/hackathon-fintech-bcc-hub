"""Compared money amounts in different units: a finding from code, without a model's verdict."""

import json
from dataclasses import replace

from fakes import fake_team
from test_facts import DOC, _facts_from_prompt

from reqaudit.config import Settings
from reqaudit.models import Fact, FactTable, FindingStatus, Severity, SourceFormat
from reqaudit.pipeline import run_pipeline
from reqaudit.reading.text import read_text_document
from reqaudit.stages.s3_facts import find_conflicts, normalize, unit_sources
from reqaudit.units import mismatched_money, unit_findings


def fact(fid, entity, attribute, value, label=None, quote=None):
    return Fact(id=fid, entity=entity, entity_label=label or entity, attribute=attribute,
                value=value, normalized=value if attribute in ("compared_with", "derived_from")
                else normalize(value), block_id=f"b{int(fid[2:]):04d}",
                quote=quote or f"цитата {fid}")


def table(*facts: Fact) -> FactTable:
    return FactTable(facts=list(facts), conflicts=find_conflicts(list(facts)))


# The organisers' document: purchases are stored in tiyn, the threshold in tenge, and the
# monthly spend built from the purchases is compared with the threshold.
SPEND = [
    fact("F-001", "monthly_spend", "compared_with", "spend_threshold", "расходы"),
    fact("F-002", "monthly_spend", "derived_from", "purchase_amount", "расходы"),
    fact("F-003", "purchase_amount", "unit", "в тиынах", "сумма покупки"),
    fact("F-004", "spend_threshold", "unit", "в тенге", "порог"),
]


def test_units_written_with_a_preposition_are_recognised():
    assert normalize("в тиынах") == "tiyn" and normalize("в тенге") == normalize("₸") == "kzt"
    assert normalize("по Алматы") == normalize("в Алматы")
    assert normalize("от 1 000 до 1 000 000") == normalize("1 000–1 000 000") == "1000-1000000"
    assert normalize("«0» или «1»") == normalize("0 или 1")


def test_unit_comes_from_the_quantity_itself_or_from_what_it_is_built_from():
    assert [f.id for f in unit_sources("monthly_spend", SPEND)] == ["F-003"]
    assert [f.id for f in unit_sources("spend_threshold", SPEND)] == ["F-004"]
    own = [*SPEND, fact("F-005", "monthly_spend", "unit", "тенге")]
    assert [f.id for f in unit_sources("monthly_spend", own)] == ["F-005"]  # its own wins


def test_tiyn_against_tenge_is_a_confirmed_finding_quoting_both_units():
    findings = unit_findings(table(*SPEND), [], [])
    assert len(findings) == 1
    f = findings[0]
    assert f.status == FindingStatus.CONFIRMED and f.severity == Severity.MAJOR
    assert f.criterion == "set_consistent" and f.trail == ["code:units"] and f.question
    assert "тиынах" in f.title and "тенге" in f.title
    assert [e.block_id for e in f.evidence] == ["b0003", "b0004", "b0001"]
    assert all(e.verified for e in f.evidence)


def test_same_unit_in_other_words_or_a_shared_unit_is_not_a_finding():
    same = [*SPEND[:2], fact("F-003", "purchase_amount", "unit", "₸"), SPEND[3]]
    assert unit_findings(table(*same), [], []) == []
    # The threshold is stated in both units somewhere: the two sides can be compared.
    both = [*SPEND, fact("F-005", "spend_threshold", "unit", "тиынах")]
    assert mismatched_money(table(*both)) == []


def test_only_compared_amounts_and_only_money():
    unlinked = [SPEND[2], SPEND[3]]  # two units in the document, nothing compares them
    assert unit_findings(table(*unlinked), [], []) == []
    time = [fact("F-001", "push_delay", "compared_with", "sla"),
            fact("F-002", "push_delay", "unit", "секунд"),
            fact("F-003", "sla", "unit", "минут")]
    assert unit_findings(table(*time), [], []) == []
    assert unit_findings(None, [], []) == []


def test_a_conversion_mentioned_in_the_document_leaves_the_case_to_the_reviewer():
    doc = read_text_document("Сумма в тиынах делится на 100 перед сравнением с порогом.\n",
                             SourceFormat.MARKDOWN)
    assert unit_findings(table(*SPEND), doc.blocks, []) == []
    plain = read_text_document("Расходы сравниваются с порогом.\n", SourceFormat.MARKDOWN)
    assert len(unit_findings(table(*SPEND), plain.blocks, [])) == 1


def test_finding_reaches_the_report_even_when_the_reviewer_reports_nothing(tmp_path):
    model, _ = fake_team(facts=_facts_from_prompt)
    source = tmp_path / "spec.md"
    source.write_text(DOC, encoding="utf-8")
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(source, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    mine = [f for f in state["findings"] if "code:units" in f["trail"]]
    assert len(mine) == 1 and mine[0]["id"].startswith("D-")
    assert {e["quote"] for e in mine[0]["evidence"]} >= {
        "Сумма каждой покупки хранится в тиынах", "Порог задаётся целым числом тенге"}
    assert "сравниваются без правила пересчёта" in report
    assert "такую находку выносит сам код" in report
