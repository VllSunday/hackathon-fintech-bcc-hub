from pathlib import Path

from reqaudit.contract import contract_findings, find_type_conflicts, row_quote
from reqaudit.evidence import quote_in
from reqaudit.models import FindingStatus, Severity, SourceFormat
from reqaudit.reading.text import read_text_document

CORPUS = Path(__file__).parents[1] / "corpus" / "inputs"
ORGANIZERS_DOC = CORPUS / "organizers" / "requirements.md"


def md(text: str):
    return read_text_document(text, SourceFormat.MARKDOWN)


def test_organizers_document_types_differ_between_query_and_response():
    doc = md(ORGANIZERS_DOC.read_text(encoding="utf-8"))
    conflicts = find_type_conflicts(doc.blocks)
    assert sorted(c.field for c in conflicts) == ["amount", "enabled"]
    for c in conflicts:
        assert (c.first.family, c.second.family) == ("integer", "string")


def test_finding_quotes_both_rows_and_is_a_confirmed_fact():
    doc = md(ORGANIZERS_DOC.read_text(encoding="utf-8"))
    findings = contract_findings(doc.blocks, [])
    # amount and enabled differ the same way between the same two tables: one finding,
    # one question, not a near-identical pair.
    assert len(findings) == 1
    for f in findings:
        assert f.status == FindingStatus.CONFIRMED and f.severity == Severity.MAJOR
        assert f.criterion == "set_consistent" and f.question and f.trail == ["code:contract"]
        assert len({e.block_id for e in f.evidence}) == 4
        assert f.title.startswith("Поля «amount», «enabled»: integer")
        assert "«amount», «enabled»" in f.question
        blocks = {b.id: b for b in doc.blocks}
        for e in f.evidence:  # the report and the UI find the quote in the document
            assert quote_in(e.quote, [blocks[e.block_id]])


def test_no_false_alarms_on_the_rest_of_the_corpus():
    for path in sorted(CORPUS.rglob("*.md")):
        if path == ORGANIZERS_DOC:
            continue
        doc = md(path.read_text(encoding="utf-8"))
        assert find_type_conflicts(doc.blocks) == [], path.name


_TWO_METHODS = """# API

## Метод 1

### Параметры запроса

| Parameter | Format | Description |
| --- | --- | --- |
| `id` | integer | Номер |

### Ответ

| Parameter | Format | Description |
| --- | --- | --- |
| `id` | integer | Номер |

## Метод 2

### Параметры запроса

| Parameter | Format | Description |
| --- | --- | --- |
| `id` | string | Код |
"""


def test_same_name_in_another_method_is_another_field():
    assert find_type_conflicts(md(_TWO_METHODS).blocks) == []


def test_same_name_in_one_method_with_two_types_is_a_conflict():
    text = _TWO_METHODS.replace("| `id` | integer | Номер |\n\n## Метод 2",
                                "| `id` | string | Номер |\n\n## Метод 2")
    conflicts = find_type_conflicts(md(text).blocks)
    assert [c.field for c in conflicts] == ["id"]


def test_unknown_format_words_and_sizes_are_not_compared():
    text = """## API

### Запрос

| Parameter | Format | DB field |
| --- | --- | --- |
| `sum` | int64 | `amount` |
| `note` | см. справочник | `note` |

### Ответ

| Parameter | Format | DB field |
| --- | --- | --- |
| `sum` | integer (32 bit) | `amount` |
| `note` | string | `note` |
"""
    assert find_type_conflicts(md(text).blocks) == []


def test_row_quote_reads_as_text():
    doc = md(ORGANIZERS_DOC.read_text(encoding="utf-8"))
    row = find_type_conflicts(doc.blocks)[0].first.block
    assert "|" not in row_quote(row) and "`" not in row_quote(row)


def test_type_conflict_reaches_the_report_past_the_opponent(tmp_path):
    import json
    from dataclasses import replace

    from fakes import fake_team

    from reqaudit.config import Settings
    from reqaudit.pipeline import run_pipeline

    model, _ = fake_team(refute=lambda title: True)  # the opponent refutes every finding
    settings = replace(Settings(), runs_dir=tmp_path / "runs", cache_dir=tmp_path / "cache",
                       models={"judge": ["fake:team"]})
    result = run_pipeline(ORGANIZERS_DOC, tmp_path / "report.md", settings,
                          model_factory=lambda _id: model)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    contract = [f for f in state["findings"] if f["trail"][:1] == ["code:contract"]]
    assert [f["title"].split(":")[0] for f in contract] == ["Поля «amount», «enabled»"]
    assert all(f["unit_ids"] for f in contract)  # tied to the API requirements
    asked = {i for q in state["questions"] for i in q["finding_ids"]}
    assert {f["id"] for f in contract} <= asked
    assert "Поля «amount», «enabled»" in report
