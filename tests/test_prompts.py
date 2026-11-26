import pytest

from reqaudit import criteria, prompt_store
from reqaudit.criteria import Criterion, Level


def test_system_base_renders_without_placeholders():
    text = prompt_store.render("system_base")
    assert "{{" not in text
    assert "suspicious_content" in text


def test_missing_placeholder_value_fails(tmp_path, monkeypatch):
    (tmp_path / "probe.md").write_text("---\nid: probe\nversion: 1\n---\nHi {{name}}\n",
                                       encoding="utf-8")
    monkeypatch.setattr(prompt_store, "PROMPTS_DIR", tmp_path)
    assert prompt_store.render("probe", name="x") == "Hi x\n"
    with pytest.raises(KeyError):
        prompt_store.render("probe")


def test_fingerprint_is_stable():
    assert prompt_store.fingerprint() == prompt_store.fingerprint()


def test_every_criterion_has_label_and_definition():
    for code in Criterion:
        info = criteria.CRITERIA[code]
        assert info.label_ru and info.definition and info.severity


def test_criterion_cards_carry_how_to_check_and_their_limits():
    """The model gets working knowledge, not references: tests, limits and both standards."""
    card = criteria.card(Criterion.UNAMBIGUOUS)
    assert card.startswith("#### `unambiguous` — ISO 29148 5.2.5; IEEE 830 4.3.2")
    for part in ("Check:", "Typical violation:", "Not a violation:", "Boundary:", "Severity:"):
        assert part in card
    for code in (Criterion.SET_CONSISTENT, Criterion.VERIFIABLE, Criterion.COMPLETE,
                 Criterion.SET_COMPLETE, Criterion.CORRECT):
        assert "IEEE 830 4.3." in criteria.CRITERIA[code].source
    # The task's three kinds of critical defects map onto the standards' criteria.
    kinds = {info.tz_type for info in criteria.CRITERIA.values()} - {None}
    assert kinds == set(criteria.TZ_TYPES)
    assert {i.level for i in criteria.CRITERIA.values()} == set(Level)


def test_one_severity_scale_for_every_role_that_sets_severity():
    for name in ("s5_common", "s6_opponent", "s6_arbiter", "s8b_reassess"):
        assert "severity scale" in prompt_store.load(name).body
    text, ids = prompt_store.compose("s6_opponent", "severity", legend="")
    assert "a stub or a pick is a guess" in text and any(i.startswith("severity@") for i in ids)
