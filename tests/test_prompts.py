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
        assert info.label_ru and info.definition
    assert "`unambiguous`" in criteria.prompt_block(Level.REQUIREMENT)
