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


def test_google_cloud_key_chooses_vertex(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.test-cloud-key")
    assert load_settings().chain_for("judge") == ["vertex:gemini-3.1-pro-preview",
                                                  "vertex:gemini-3.5-flash"]
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-studio-key")
    assert load_settings().chain_for("judge") == ["gemini:gemini-3.5-flash-lite"]


def test_vertex_model_is_built_for_google_cloud(monkeypatch):
    from pydantic_ai.models.google import GoogleModel

    from reqaudit.llm.client import default_model

    monkeypatch.setenv("VERTEX_API_KEY", "AQ.test-cloud-key")
    model = default_model("vertex:gemini-3.5-flash")
    assert isinstance(model, GoogleModel) and model.model_name == "gemini-3.5-flash"
    assert default_model("openai:gpt-5.5") == "openai:gpt-5.5"


def test_vertex_client_is_not_shared_between_calls(monkeypatch):
    """Each synchronous call runs in its own event loop; a shared HTTP client would be
    bound to the first, closed loop ("Event loop is closed")."""
    from reqaudit.llm.client import default_model

    monkeypatch.setenv("VERTEX_API_KEY", "AQ.test-cloud-key")
    first = default_model("vertex:gemini-3.5-flash")
    second = default_model("vertex:gemini-3.5-flash")
    assert first._provider is not second._provider


def test_gemini_model_falls_back_to_vertex_with_a_cloud_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.test-cloud-key")
    monkeypatch.setenv("REQAUDIT_MODEL_JUDGE", "gemini:gemini-3.5-flash")
    settings = load_settings()
    assert settings.chain_for("judge") == ["gemini:gemini-3.5-flash", "vertex:gemini-3.5-flash"]
    assert settings.chain_for("vision") == settings.chain_for("judge")  # vision falls back to judge
    assert any("Vertex" in n for n in settings.notes)
