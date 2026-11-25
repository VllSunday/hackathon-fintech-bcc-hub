"""Fake models for tests: deterministic stand-ins that answer by the output schema."""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

_BLOCK_LINE = re.compile(r"^\[(b\d{4})\] (\w+): (.*)$", re.MULTILINE)


def user_prompt(messages) -> str:
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return part.content
    return ""


def honest_segmentation(prompt: str) -> dict:
    """List items and table rows become units; other non-heading blocks are skipped."""
    units, skipped = [], []
    for block_id, tag, text in _BLOCK_LINE.findall(prompt):
        if tag in ("H", "TH"):
            continue
        if not text.strip(" |"):
            skipped.append({"block_id": block_id, "reason": "empty"})
        elif tag in ("LI", "TR"):
            units.append({"block_ids": [block_id], "doc_label": None, "kind": "functional",
                          "quote": text, "summary": "Требование из документа"})
        else:
            skipped.append({"block_id": block_id, "reason": "context"})
    return {"notes": "Требования заданы списками и таблицами.", "units": units,
            "skipped": skipped}


def fake_analyst(mutate: Callable[[dict, int], dict] | None = None):
    """A model that profiles and segments by block tags.

    `mutate(answer, n)` may corrupt the n-th segmentation answer (1-based) to test checks.
    Returns the model and a counter dict.
    """
    calls = {"profile": 0, "segment": 0}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0]
        prompt = user_prompt(messages)
        if "parts" in tool.parameters_json_schema.get("properties", {}):
            calls["profile"] += 1
            first = _BLOCK_LINE.search(prompt).group(1)
            args = {"summary": "Тестовый документ требований.",
                    "parts": [{"name": "Документ", "role": "main", "start_block_id": first,
                               "parent": None}],
                    "numbering": "нумерации нет", "has_acceptance_criteria": True,
                    "has_api_spec": False, "has_data_requirements": False,
                    "has_non_functional": False, "has_ui_spec": False}
        else:
            calls["segment"] += 1
            args = honest_segmentation(prompt)
            if mutate:
                args = mutate(args, calls["segment"])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args=args)])

    return FunctionModel(respond), calls
