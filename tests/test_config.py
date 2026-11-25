from reqaudit.config import load_settings


def test_model_is_chosen_by_available_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = load_settings()
    assert settings.chain_for("judge") == ["openai:gpt-5.5"]
    assert settings.chain_for("analyst") == ["openai:gpt-5.5"]
    assert any("автоматически" in n for n in settings.notes)


def test_both_keys_give_a_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    assert load_settings().chain_for("judge") == ["openai:gpt-5.5",
                                                  "gemini:gemini-3.5-flash-lite"]


def test_configured_model_without_key_is_skipped(monkeypatch):
    # The situation from .env.example: a Gemini model configured, only an OpenAI key present.
    monkeypatch.setenv("REQAUDIT_MODEL_JUDGE", "gemini:gemini-3.5-flash-lite")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = load_settings()
    assert settings.chain_for("judge") == ["openai:gpt-5.5"]
    assert any("пропущена" in n and "GEMINI_API_KEY" in n for n in settings.notes)


def test_explicit_model_with_key_wins(monkeypatch):
    monkeypatch.setenv("REQAUDIT_MODEL_JUDGE", "openai:gpt-5.4-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert load_settings().chain_for("judge") == ["openai:gpt-5.4-mini"]


def test_no_key_explains_what_to_do():
    settings = load_settings()
    assert settings.models == {}
    assert any("OPENAI_API_KEY" in n for n in settings.notes)


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.setenv("REQAUDIT_MODEL_JUDGE", "ollama:qwen3")
    assert load_settings().chain_for("judge") == ["ollama:qwen3"]
