import pytest

# Variables a developer's .env may set. Ordinary tests must never reach a real model or
# write into the real cache, whatever .env contains; live tests keep them.
_ISOLATED = [
    "REQAUDIT_MODEL_ANALYST", "REQAUDIT_MODEL_JUDGE", "REQAUDIT_MODEL_OPPONENT",
    "REQAUDIT_MODEL_VISION", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "REQAUDIT_PROFILE", "REQAUDIT_MAX_LLM_CALLS", "REQAUDIT_RPM",
]


@pytest.fixture(autouse=True)
def isolate_environment(request, monkeypatch, tmp_path):
    if request.node.get_closest_marker("live"):
        return
    for name in _ISOLATED:
        # An empty value counts as set, so python-dotenv will not fill it from .env.
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("REQAUDIT_CACHE_DIR", str(tmp_path / "llm-cache"))
