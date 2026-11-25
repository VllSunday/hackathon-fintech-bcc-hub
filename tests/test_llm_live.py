"""Real model call. Skipped by default; run with `uv run pytest -m live`.

Needs REQAUDIT_MODEL_JUDGE and the matching API key in .env.
"""

from dataclasses import replace

import pytest
from pydantic import BaseModel, Field

from reqaudit.config import load_settings
from reqaudit.llm import LLMClient, require_russian
from reqaudit.trace import Trace

pytestmark = pytest.mark.live


class Probe(BaseModel):
    requirement_count: int = Field(description="How many separate requirements the text states.")
    summary: str = Field(description="One sentence in Russian.")


def test_judge_model_answers_in_schema_and_russian(tmp_path):
    settings = load_settings()
    if not settings.models.get("judge"):
        pytest.skip("REQAUDIT_MODEL_JUDGE is not set")
    settings = replace(settings, cache_dir=tmp_path / "cache", max_llm_calls=5)
    client = LLMClient(settings, Trace(tmp_path / "trace.jsonl"))

    result = client.structured(
        step="live_probe",
        role="judge",
        instructions="Count the requirements in the user text. Answer in the schema; "
                     "write the summary in Russian.",
        prompt="Ф-1. Клиент задаёт порог от 1 000 до 1 000 000 ₸. "
               "Ф-2. Push отправляется в течение минуты после достижения порога.",
        output_type=Probe,
        check=require_russian("summary"),
    )

    assert result.requirement_count == 2
    assert client.requests >= 1
    print(f"\nmodel: {client.models_used}, summary: {result.summary}")
