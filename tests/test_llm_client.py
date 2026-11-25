from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.exceptions import ModelQuotaExceededError, ModelRateLimitError
from pydantic_ai.models.function import AgentInfo, FunctionModel

from reqaudit.config import Settings
from reqaudit.llm import BudgetExceeded, LLMClient, LLMUnavailable, require_russian
from reqaudit.llm.client import to_library_model_id
from reqaudit.trace import Trace


class Summary(BaseModel):
    summary: str
    count: int


def answering(*answers: dict | Exception):
    """A fake model that replays answers in order and counts how often it was asked."""
    state = {"calls": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        answer = answers[min(state["calls"], len(answers) - 1)]
        state["calls"] += 1
        if isinstance(answer, Exception):
            raise answer
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name,
                                                 args=answer)])

    return FunctionModel(fn), state


RU = {"summary": "Два требования к порогу", "count": 2}


class Sleeps:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def client_for(tmp_path: Path, models: dict, chain=("fake:a",), **overrides):
    settings = replace(Settings(), models={"judge": list(chain)},
                       cache_dir=tmp_path / "cache", backoff_base_s=0.0, **overrides)
    sleeps = Sleeps()
    client = LLMClient(settings, Trace(tmp_path / "trace.jsonl"),
                       model_factory=lambda model_id: models[model_id], sleep=sleeps)
    return client, sleeps


def ask(client: LLMClient, prompt: str = "Сколько требований?", **kwargs):
    return client.structured(step="test", role="judge", instructions="Answer.", prompt=prompt,
                             output_type=Summary, **kwargs)


def test_answer_is_cached(tmp_path):
    model, state = answering(RU)
    client, _ = client_for(tmp_path, {"fake:a": model})

    assert ask(client) == Summary(**RU)
    assert ask(client) == Summary(**RU)
    assert state["calls"] == 1
    assert (client.requests, client.cache_hits) == (1, 1)
    assert client.models_used == ["fake:a"]

    fresh, _ = client_for(tmp_path, {"fake:a": model}, cache_read=False)
    ask(fresh)
    assert state["calls"] == 2


def test_check_sends_complaint_and_gets_russian(tmp_path):
    model, state = answering({"summary": "Two requirements", "count": 2}, RU)
    client, _ = client_for(tmp_path, {"fake:a": model})

    result = ask(client, check=require_russian("summary"))

    assert result.summary == RU["summary"]
    assert state["calls"] == 2


def test_rate_limit_is_retried_on_same_model(tmp_path):
    model, state = answering(ModelRateLimitError("fake:a", "slow down", retry_after=1.5), RU)
    client, sleeps = client_for(tmp_path, {"fake:a": model})

    assert ask(client) == Summary(**RU)
    assert sleeps.delays == [1.5]
    assert state["calls"] == 2


def test_quota_falls_back_to_next_model(tmp_path):
    primary, _ = answering(ModelQuotaExceededError("fake:a", "daily quota"))
    backup, _ = answering(RU)
    client, _ = client_for(tmp_path, {"fake:a": primary, "fake:b": backup},
                           chain=("fake:a", "fake:b"))

    assert ask(client) == Summary(**RU)
    assert client.models_used == ["fake:b"]
    events = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"llm_fallback"' in events


def test_all_models_failing_raises(tmp_path):
    primary, _ = answering(ModelQuotaExceededError("fake:a", "daily quota"))
    client, _ = client_for(tmp_path, {"fake:a": primary})
    with pytest.raises(LLMUnavailable) as info:
        ask(client)
    assert "fake:a" in info.value.message


def test_budget_stops_further_requests(tmp_path):
    model, _ = answering(RU)
    client, _ = client_for(tmp_path, {"fake:a": model}, max_llm_calls=1)
    ask(client, prompt="первый")
    with pytest.raises(BudgetExceeded):
        ask(client, prompt="второй")


def test_missing_role_model_is_explained(tmp_path):
    client, _ = client_for(tmp_path, {}, chain=())
    with pytest.raises(LLMUnavailable) as info:
        ask(client)
    assert "REQAUDIT_MODEL_JUDGE" in info.value.message


def test_model_id_mapping():
    assert to_library_model_id("gemini:gemini-3.5-flash-lite") == "google:gemini-3.5-flash-lite"
    assert to_library_model_id("openai:some-model") == "openai:some-model"
    with pytest.raises(LLMUnavailable):
        to_library_model_id("no-provider")
