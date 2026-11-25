"""Spend controls must not change what the model is asked, only order, routing and accounting."""

from dataclasses import replace

import pytest
from fakes import user_prompt
from pydantic import BaseModel
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from reqaudit.config import Settings
from reqaudit.llm import BudgetExceeded, LLMClient
from reqaudit.llm.pricing import price_usd
from reqaudit.trace import Trace

DOCUMENT = "<document>\n" + "Текст документа. " * 400 + "\n</document>\n"


class Answer(BaseModel):
    note: str


def recording_model():
    seen: list[dict] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        prompt = user_prompt(messages)
        seen.append({"prompt": prompt, "instructions": info.instructions,
                     "settings": dict(info.model_settings or {})})
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name,
                                                 args={"note": prompt[-12:]})])

    return FunctionModel(respond), seen


def client_for(tmp_path, model, model_id="fake:a", **overrides):
    settings = replace(Settings(), models={"judge": [model_id]}, cache_dir=tmp_path / "cache",
                       **overrides)
    return LLMClient(settings, Trace(tmp_path / "trace.jsonl"),
                     model_factory=lambda _id: model)


def request(cell: str, instructions: str = "Review the requirements.") -> dict:
    return dict(step=f"s5[{cell}]", role="judge", instructions=instructions,
                prompt=f"{DOCUMENT}Check: {cell}.", output_type=Answer)


def test_requests_reach_the_model_unchanged_and_in_order(tmp_path):
    model, seen = recording_model()
    client = client_for(tmp_path, model)
    requests = [request(f"req-{n}") for n in range(1, 5)] + [request("other", "Other task.")]

    results = client.many(requests)

    # Every request was sent exactly as built: same instructions, same prompt.
    sent = {(s["instructions"], s["prompt"]) for s in seen}
    assert sent == {(r["instructions"], r["prompt"]) for r in requests}
    # Results come back in request order, whatever order the calls ran in.
    assert [r.note for r in results] == [r["prompt"][-12:] for r in requests]


def test_one_request_of_a_group_warms_the_cache_before_the_rest(tmp_path):
    model, seen = recording_model()
    client = client_for(tmp_path, model)
    group = [request(f"req-{n}") for n in range(1, 5)]
    single = request("other", "Other task.")

    client.many([*group, single])

    order = [s["prompt"][-10:] for s in seen]
    first_wave = set(order[:2])
    assert first_wave == {group[0]["prompt"][-10:], single["prompt"][-10:]}
    assert set(order[2:]) == {r["prompt"][-10:] for r in group[1:]}


def test_cache_routing_hint_only_for_openai_and_never_in_the_disk_cache_key(tmp_path):
    model, seen = recording_model()
    group = [request(f"req-{n}") for n in range(1, 4)]

    client_for(tmp_path, model, model_id="openai:gpt-test").many(group)
    keys = {s["settings"].get("openai_prompt_cache_key") for s in seen}
    assert len(keys) == 1 and next(iter(keys)).startswith("reqaudit-")

    # The same request asked alone, without a routing hint, is served from the disk cache:
    # the hint is not part of what the answer depends on.
    again = client_for(tmp_path, model, model_id="openai:gpt-test")
    again.structured(**group[0])
    assert again.cache_hits == 1 and len(seen) == 3

    seen.clear()
    client_for(tmp_path / "other", model).many(group)
    assert all("openai_prompt_cache_key" not in s["settings"] for s in seen)


def test_prices_come_from_the_catalogue():
    assert price_usd("openai:gpt-5.4-mini", 1_000_000, 0, 0) == pytest.approx(0.75)
    assert price_usd("openai:gpt-5.4-mini", 1_000_000, 1_000_000, 0) == pytest.approx(0.075)
    assert price_usd("fake:a", 1000, 0, 1000) is None


def test_cost_ceiling_stops_new_requests(tmp_path):
    model, seen = recording_model()
    client = client_for(tmp_path, model, max_cost_usd=1.0)
    client.cost_usd = 1.5
    with pytest.raises(BudgetExceeded) as info:
        client.structured(**request("req-1"))
    assert "REQAUDIT_MAX_COST_USD" in info.value.message
    assert seen == []


def test_spend_is_counted(tmp_path):
    model, _ = recording_model()
    client = client_for(tmp_path, model)
    client.structured(**request("req-1"))
    assert client.input_tokens > 0 and client.output_tokens > 0
    assert client.unpriced_calls == 1 and client.cost_usd == 0.0
