"""s3: facts are lined up by code; conflicts are candidates for the values reviewer."""

import json
import re
from dataclasses import replace

from fakes import _BLOCK_LINE, fake_team, honest_set_review

from reqaudit.config import Settings
from reqaudit.models import Fact
from reqaudit.pipeline import run_pipeline
from reqaudit.stages.s3_facts import find_conflicts, normalize

DOC = """# Уведомление о расходах

## Требования

* Ф-1. Расходы за месяц сравниваются с порогом при каждой покупке.
* Ф-2. Порог задаётся целым числом тенге.
* Ф-3. Сумма каждой покупки хранится в тиынах.

## API

| Parameter | Format | Description |
|---|---|---|
| amount | integer | Порог в запросе |
| amount | string | Порог в ответе |
"""


def fact(fid, entity, attribute, value, condition=None, label=None):
    return Fact(id=fid, entity=entity, entity_label=label or entity, attribute=attribute,
                value=value, normalized=value if attribute in ("compared_with", "derived_from")
                else normalize(value), condition=condition, block_id="b0001", quote="q")


def test_normalize_units_types_and_numbers():
    assert normalize("₸") == normalize("тенге") == normalize("KZT") == "kzt"
    assert normalize("тиынах") == "tiyn"
    assert normalize("integer") == normalize("целое число") == "integer"
    assert normalize("1 000–1 000 000") == "1000-1000000"
    assert normalize("Да") == normalize("обязательный") == "yes"


def test_unit_mismatch_follows_derived_from():
    facts = [
        fact("F-001", "monthly_spend", "compared_with", "spend_threshold",
             label="расходы за месяц"),
        fact("F-002", "monthly_spend", "derived_from", "purchase_amount"),
        fact("F-003", "purchase_amount", "unit", "тиынах", label="сумма покупки"),
        fact("F-004", "spend_threshold", "unit", "тенге", label="порог"),
    ]
    conflicts = {c.kind: c for c in find_conflicts(facts)}
    assert set(conflicts) == {"unit_mismatch", "mixed_units"}
    mismatch = conflicts["unit_mismatch"]
    assert set(mismatch.fact_ids) == {"F-001", "F-002", "F-003", "F-004"}
    assert "tiyn" in mismatch.summary and "kzt" in mismatch.summary


def test_same_unit_in_different_words_is_not_a_conflict():
    facts = [fact("F-001", "a", "compared_with", "b"), fact("F-002", "a", "unit", "₸"),
             fact("F-003", "b", "unit", "тенге")]
    assert find_conflicts(facts) == []


def test_different_values_unless_each_has_its_own_condition():
    typed = [fact("F-001", "spend_threshold", "type", "integer"),
             fact("F-002", "spend_threshold", "type", "string")]
    assert [c.kind for c in find_conflicts(typed)] == ["different_values"]

    examples = [fact("F-001", "spend_threshold", "value", "50 000", "критерий приёмки 1"),
                fact("F-002", "spend_threshold", "value", "20 000", "критерий приёмки 3")]
    assert find_conflicts(examples) == []

    rule_vs_example = [fact("F-001", "purchase_month", "period", "по дате покупки"),
                       fact("F-002", "purchase_month", "period", "по дате проведения",
                            "критерий приёмки 2")]
    assert len(find_conflicts(rule_vs_example)) == 1


def test_mixed_units_without_any_links_between_quantities():
    # Observed with a real model: the units sit on entities it never linked to each other.
    facts = [fact("F-001", "purchase_amount", "unit", "тиынах", label="сумма покупки"),
             fact("F-002", "spend_threshold", "unit", "тенге", label="порог"),
             fact("F-003", "card_currency", "unit", "KZT", label="карта")]
    conflicts = find_conflicts(facts)
    assert [c.kind for c in conflicts] == ["mixed_units"]
    assert set(conflicts[0].fact_ids) == {"F-001", "F-002", "F-003"}


def test_same_name_different_entities_are_compared():
    # Observed: request and response `amount` got different keys from the model.
    facts = [fact("F-001", "spend_threshold", "type", "integer", label="amount"),
             fact("F-002", "response_amount", "type", "string", label="amount")]
    conflicts = find_conflicts(facts)
    assert len(conflicts) == 1 and conflicts[0].kind == "different_values"


def test_rows_of_one_example_table_are_not_conflicts():
    facts = [fact("F-001", "spend", "value", "48 500 ₸"),
             fact("F-002", "spend", "value", "19 900 ₸")]
    facts[1] = facts[1].model_copy(update={"block_id": "b0002"})
    assert find_conflicts(facts, {"b0001": "t1", "b0002": "t1"}) == []
    assert len(find_conflicts(facts, {"b0001": "t1", "b0002": "t2"})) == 1


def test_failed_fact_extraction_does_not_stop_the_other_checks(tmp_path):
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.models.function import FunctionModel

    model, calls = fake_team()

    def facts_down(messages, info):
        # A bad answer for this step only; quota errors would rightly disable the whole model.
        if set(info.output_tools[0].parameters_json_schema["properties"]) == {"facts"}:
            raise UnexpectedModelBehavior("unusable answer")
        return model.function(messages, info)

    source = tmp_path / "spec.md"
    source.write_text(DOC, encoding="utf-8")
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(source, tmp_path / "report.md", settings,
                          model_factory=lambda _id: FunctionModel(facts_down))
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))

    assert result.exit_code == 3 and result.status == "checks_incomplete"
    assert calls["review"] > 0 and state["findings"]  # the rest of the checks ran
    assert any(f["cell"] == "Таблица фактов и сверка значений" for f in state["check_failures"])
    assert all(c["kind"] != "values" for c in state["cells"])


def _facts_from_prompt(prompt: str) -> dict:
    """A fake extractor that reads the test document the way a model would."""
    ids = {text: bid for bid, _, text in _BLOCK_LINE.findall(prompt)}

    def block(fragment):
        return next(bid for text, bid in ids.items() if fragment in text)

    rows = [(bid, text) for bid, tag, text in _BLOCK_LINE.findall(prompt) if tag == "TR"]
    return {"facts": [
        {"entity": "monthly_spend", "entity_label": "расходы за месяц",
         "attribute": "compared_with", "value": "spend_threshold",
         "block_id": block("Ф-1"), "quote": "Расходы за месяц сравниваются с порогом"},
        {"entity": "monthly_spend", "entity_label": "расходы за месяц",
         "attribute": "derived_from", "value": "purchase_amount",
         "block_id": block("Ф-1"), "quote": "Расходы за месяц сравниваются с порогом"},
        {"entity": "spend_threshold", "entity_label": "порог", "attribute": "unit",
         "value": "тенге", "block_id": block("Ф-2"), "quote": "Порог задаётся целым числом тенге"},
        {"entity": "purchase_amount", "entity_label": "сумма покупки", "attribute": "unit",
         "value": "тиынах", "block_id": block("Ф-3"),
         "quote": "Сумма каждой покупки хранится в тиынах"},
        {"entity": "spend_threshold", "entity_label": "amount", "attribute": "type",
         "value": "integer", "block_id": rows[0][0], "quote": rows[0][1].strip("| ")},
        {"entity": "spend_threshold", "entity_label": "amount", "attribute": "type",
         "value": "string", "block_id": rows[1][0], "quote": rows[1][1].strip("| ")},
        {"entity": "spend_threshold", "entity_label": "порог", "attribute": "unit",
         "value": "рублях", "block_id": block("Ф-2"), "quote": "Этой цитаты в документе нет"},
    ]}


def test_values_cell_gets_the_fact_table_and_code_conflicts(tmp_path):
    seen_prompts = []

    def values_review(prompt):
        if "Check: Сверка значений." in prompt:
            seen_prompts.append(prompt)
        return honest_set_review(prompt)

    model, calls = fake_team(facts=_facts_from_prompt, set_review=values_review)
    source = tmp_path / "spec.md"
    source.write_text(DOC, encoding="utf-8")
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(source, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert calls["facts"] >= 1
    table = state["facts"]
    assert len(table["facts"]) == 6
    assert table["dropped"] == 1  # the fact with an invented quote never enters the table
    kinds = sorted(c["kind"] for c in table["conflicts"])
    assert kinds == ["different_values", "mixed_units", "unit_mismatch"]

    assert len(seen_prompts) == 1
    prompt = seen_prompts[0]
    assert "<fact_table>" in prompt and "unit_mismatch" in prompt
    assert re.search(r"F-\d{3} type = string", prompt)
    assert "сверил их кодом" in report and "единицы различаются" in report


def test_units_code_does_not_know_are_compared_by_the_models_dimension():
    """«МБ» and «ГБ» are not in code's dictionary; the model's dimension places them."""
    upload = fact("F-001", "scan_upload", "unit", "МБ").model_copy(
        update={"dimension": "data_size"})
    archive = fact("F-002", "archive_size", "unit", "ГБ").model_copy(
        update={"dimension": "data_size"})
    attempts = fact("F-003", "retry", "unit", "попыток").model_copy(update={"dimension": "count"})
    tries = fact("F-004", "otp", "unit", "раз").model_copy(update={"dimension": "count"})
    kinds = [(c.kind, set(c.fact_ids)) for c in find_conflicts([upload, archive, attempts, tries])]
    assert ("mixed_units", {"F-001", "F-002"}) in kinds
    assert all("F-003" not in ids for _, ids in kinds)  # counting units are never mixed
