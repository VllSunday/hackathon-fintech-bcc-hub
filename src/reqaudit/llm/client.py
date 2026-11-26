"""One place for every model call: cache, trace, budget, throttling, retries and fallback.

Model ids use our own format `provider:model`, where provider is gemini (AI Studio),
vertex (Gemini on Google Cloud), openai, anthropic or ollama. A role maps to an ordered
chain of model ids; the first one that answers wins. The library underneath is
PydanticAI, hidden behind this class so it can be replaced without touching the stages.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from google.genai import Client as GenaiClient  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402
from pydantic_ai import (  # noqa: E402
    Agent,
    BinaryContent,
    ModelRetry,
    RunContext,
    capture_run_messages,
)
from pydantic_ai import exceptions as pai  # noqa: E402
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models import Model  # noqa: E402
from pydantic_ai.models.google import GoogleModel  # noqa: E402
from pydantic_ai.providers.google import GoogleProvider  # noqa: E402

from reqaudit.config import Settings, provider_key  # noqa: E402
from reqaudit.llm.cache import DiskCache, cache_key  # noqa: E402
from reqaudit.llm.checks import Check  # noqa: E402
from reqaudit.llm.pricing import price_usd  # noqa: E402
from reqaudit.trace import Trace  # noqa: E402

T = TypeVar("T", bound=BaseModel)
ModelFactory = Callable[[str], "Model | str"]

_PROVIDER_ALIASES = {"gemini": "google"}

# Transient: wait and try the same model again.
_RETRYABLE = (pai.ModelRateLimitError, pai.ModelUnavailableError, pai.ModelServerError,
              pai.ModelConnectionError, pai.ModelRequestTimeout)
# Persistent for this model: move on to the next model in the chain.
_FALL_THROUGH = (pai.ModelAPIError, pai.UnexpectedModelBehavior)


class LLMError(Exception):
    """A model problem the run cannot work around. `message` is user-facing Russian text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BudgetExceeded(LLMError):
    pass


class LLMUnavailable(LLMError):
    pass


def default_model(model_id: str) -> Model | str:
    """What PydanticAI needs for one of our model ids: a model object or its own id."""
    provider, _, name = model_id.partition(":")
    if provider == "vertex" and name:
        found = provider_key("vertex")
        if found is None:
            raise LLMUnavailable("Для vertex:… задайте VERTEX_API_KEY (ключ Google Cloud).")
        return GoogleModel(name, provider=_vertex_provider(found[1]))
    return to_library_model_id(model_id)


def _vertex_provider(api_key: str) -> GoogleProvider:
    # Vertex AI express mode: a Google Cloud API key, no project or service account files.
    # A new client per model: its HTTP client belongs to the event loop it was first used
    # in, and every synchronous call runs in a loop of its own.
    return GoogleProvider(client=GenaiClient(vertexai=True, api_key=api_key))


def to_library_model_id(model_id: str) -> str:
    provider, sep, name = model_id.partition(":")
    if not sep or not name:
        raise LLMUnavailable(f"Имя модели «{model_id}» должно иметь вид provider:model, "
                             "например gemini:gemini-3.5-flash-lite.")
    return f"{_PROVIDER_ALIASES.get(provider, provider)}:{name}"


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        trace: Trace,
        model_factory: ModelFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.trace = trace
        self.cache = DiskCache(settings.cache_dir)
        self._factory = model_factory or default_model
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        # Models that ran out of quota or rejected the key; skipped for the rest of the run.
        self._unavailable: dict[str, str] = {}
        self.requests = 0
        self.cache_hits = 0
        self.models_used: list[str] = []
        # Spend of this run: tokens as providers report them, and the catalogue price.
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.unpriced_calls = 0

    # --- public -------------------------------------------------------------------------

    def structured(self, **kwargs: Any) -> Any:
        """Synchronous wrapper around `astructured` for sequential stages."""
        return asyncio.run(self.astructured(**kwargs))

    def many(self, requests: Sequence[dict[str, Any]]) -> list[Any]:
        """Run several `astructured` calls at once, at most `settings.concurrency` together.

        Results come back in request order. A call that fails yields its `LLMError` in place
        of a result, so one failed cell does not cancel the others.
        """
        return asyncio.run(self._amany(requests))

    async def _amany(self, requests: Sequence[dict[str, Any]]) -> list[Any]:
        """Parallel calls, ordered so the provider's prompt cache can work.

        Providers cache a prompt's prefix only after a request with it has been processed;
        requests sent at the same moment all miss. Requests that share instructions and the
        start of the prompt (cells of one kind over the same document) form a group: with
        three or more, one goes first and the rest follow, reading the prefix from the cache.
        The requests themselves are unchanged; only their order and a routing hint differ.
        """
        gate = asyncio.Semaphore(self.settings.concurrency)
        results: list[Any] = [None] * len(requests)

        groups: dict[str, list[int]] = {}
        for i, r in enumerate(requests):
            groups.setdefault(_prefix_key(r), []).append(i)
        first_wave, second_wave = [], []
        for key, members in groups.items():
            if len(members) >= _WARM_UP_MIN_GROUP:
                first_wave.append((members[0], key))
                second_wave += [(i, key) for i in members[1:]]
            else:
                first_wave += [(i, None) for i in members]

        async def one(index: int, group: str | None) -> None:
            async with gate:
                try:
                    results[index] = await self.astructured(**requests[index],
                                                            provider_cache_key=group)
                except LLMError as err:
                    results[index] = err

        await asyncio.gather(*(one(i, g) for i, g in first_wave))
        await asyncio.gather(*(one(i, g) for i, g in second_wave))
        return results

    async def astructured(
        self,
        *,
        step: str,
        role: str,
        instructions: str,
        prompt: str,
        output_type: type[T],
        prompt_ids: Sequence[str] = (),
        check: Check | None = None,
        soft_check: bool = True,
        model_settings: dict[str, Any] | None = None,
        images: Sequence[bytes] = (),
        provider_cache_key: str | None = None,
    ) -> T:
        """Ask the role's model for a typed answer.

        `check` returns a complaint or None; a complaint goes back to the model as a retry.
        With `soft_check`, the answer is accepted after the last retry even if the complaint
        stands, and the caller's own code is expected to drop what is still wrong. Without
        it, the model is treated as failed and the next model in the chain is tried.
        """
        chain = self.settings.chain_for(role)
        if not chain:
            raise LLMUnavailable(
                "Нет модели для проверки: задайте OPENAI_API_KEY или GEMINI_API_KEY в файле "
                f".env (модель выберется сама) или укажите её в REQAUDIT_MODEL_{role.upper()}.")

        settings = {"timeout": self.settings.request_timeout_s, **(model_settings or {})}
        schema = output_type.model_json_schema()
        # Images are PNG bytes; their hashes make the cache key depend on the pictures too.
        image_keys = "".join(f"\n<image {hashlib.sha256(i).hexdigest()}>" for i in images)
        keys = {m: cache_key(m, instructions, prompt + image_keys, schema, settings)
                for m in chain}
        content: str | list[Any] = prompt
        if images:
            content = [prompt, *(BinaryContent(data=i, media_type="image/png") for i in images)]

        if self.settings.cache_read:
            for model_id in chain:
                cached = self._from_cache(keys[model_id], output_type)
                if cached is not None:
                    self.cache_hits += 1
                    self._note_model(model_id)
                    self.trace.event("llm_call", step=step, role=role, model=model_id,
                                     cache_hit=True, key=keys[model_id][:16],
                                     prompt_ids=list(prompt_ids))
                    return cached

        failures: list[str] = []
        for model_id in chain:
            if model_id in self._unavailable:
                failures.append(f"{model_id}: {self._unavailable[model_id]} ранее в этом прогоне")
                continue
            call_settings = dict(settings)
            if provider_cache_key and model_id.startswith("openai:"):
                # Routing hint only: sends requests with one prefix to where it is cached.
                # Not part of the disk cache key, because it does not change the answer.
                call_settings["openai_prompt_cache_key"] = provider_cache_key
            try:
                output, usage, attempts, seconds = await self._call(
                    model_id, instructions, content, output_type, check, soft_check,
                    call_settings, step)
            except _ModelGaveUp as err:
                failures.append(f"{model_id}: {err}")
                self.trace.event("llm_fallback", step=step, role=role, model=model_id,
                                 reason=str(err))
                continue

            self.cache.put(keys[model_id], {
                "model": model_id,
                "step": step,
                "prompt_ids": list(prompt_ids),
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "output": output.model_dump(mode="json"),
                "usage": {"requests": usage.requests, "input_tokens": usage.input_tokens,
                          "output_tokens": usage.output_tokens},
            })
            self._note_model(model_id)
            cached = usage.cache_read_tokens or 0
            cost = price_usd(model_id, usage.input_tokens, cached, usage.output_tokens)
            self.input_tokens += usage.input_tokens
            self.cached_tokens += cached
            self.output_tokens += usage.output_tokens
            if cost is None:
                self.unpriced_calls += 1
            else:
                self.cost_usd += cost
            self.trace.event("llm_call", step=step, role=role, model=model_id, cache_hit=False,
                             key=keys[model_id][:16], prompt_ids=list(prompt_ids),
                             attempts=attempts, seconds=round(seconds, 2),
                             requests=usage.requests, input_tokens=usage.input_tokens,
                             cached_tokens=cached, output_tokens=usage.output_tokens,
                             cost_usd=round(cost, 4) if cost is not None else None)
            return output

        raise LLMUnavailable(f"Ни одна модель для роли «{role}» не ответила. "
                             + "; ".join(failures))

    # --- internals ----------------------------------------------------------------------

    def _from_cache(self, key: str, output_type: type[T]) -> T | None:
        record = self.cache.get(key)
        if record is None:
            return None
        try:
            return output_type.model_validate(record["output"])
        except (ValidationError, KeyError):
            return None

    def _note_model(self, model_id: str) -> None:
        if model_id not in self.models_used:
            self.models_used.append(model_id)

    def _reserve(self) -> None:
        limit = self.settings.max_cost_usd
        if limit is not None and self.cost_usd >= limit:
            raise BudgetExceeded(
                f"Достигнут потолок стоимости прогона ${limit:.2f} (потрачено "
                f"${self.cost_usd:.2f}). Поднимите REQAUDIT_MAX_COST_USD или повторите прогон: "
                "готовые ответы возьмутся из кэша.")
        if self.requests >= self.settings.max_llm_calls:
            raise BudgetExceeded(
                f"Исчерпан бюджет запросов к модели на прогон: {self.settings.max_llm_calls}. "
                "Поднимите REQAUDIT_MAX_LLM_CALLS или повторите прогон: готовые ответы "
                "возьмутся из кэша.")

    def usage(self) -> dict[str, object]:
        """Spend of this run under the passport's field names."""
        return {"llm_calls": self.requests, "llm_cache_hits": self.cache_hits,
                "llm_input_tokens": self.input_tokens, "llm_cached_tokens": self.cached_tokens,
                "llm_output_tokens": self.output_tokens, "llm_cost_usd": round(self.cost_usd, 4),
                "llm_unpriced_calls": self.unpriced_calls, "models_used": list(self.models_used)}

    async def _throttle(self, model_id: str) -> None:
        rpm = self.settings.requests_per_minute
        if not rpm:
            return
        provider = model_id.partition(":")[0]
        wait = self._last_request.get(provider, 0.0) + 60.0 / rpm - time.monotonic()
        if wait > 0:
            await self._sleep(wait)
        self._last_request[provider] = time.monotonic()

    async def _call(self, model_id: str, instructions: str, prompt: str | list[Any],
                    output_type: type[T],
                    check: Check | None, soft_check: bool, settings: dict[str, Any],
                    step: str):
        try:
            model = self._factory(model_id)
            agent = Agent(model, output_type=output_type, instructions=instructions,
                          retries=self.settings.output_retries)
        except (pai.UserError, LLMUnavailable, ValueError) as err:
            raise _ModelGaveUp(f"модель не создана: {getattr(err, 'message', err)}") from err

        if check is not None:
            @agent.output_validator
            def _validate(ctx: RunContext, output: T) -> T:
                complaint = check(output)
                if not complaint:
                    return output
                if soft_check and ctx.retry >= ctx.max_retries:
                    self.trace.event("llm_check_unresolved", step=step, model=model_id,
                                     complaint=complaint[:1000])
                    return output
                self.trace.event("llm_check_retry", step=step, model=model_id,
                                 retry=ctx.retry, complaint=complaint[:1000])
                raise ModelRetry(complaint)

        attempt = 0
        while True:
            attempt += 1
            self._reserve()
            await self._throttle(model_id)
            started = time.perf_counter()
            with capture_run_messages() as messages:
                try:
                    result = await agent.run(prompt, model_settings=settings)
                except _RETRYABLE as err:
                    self.requests += 1
                    if attempt >= self.settings.max_attempts:
                        raise _ModelGaveUp(f"{type(err).__name__} после {attempt} попыток") from err
                    await self._backoff(err, attempt, step, model_id)
                    continue
                except _FALL_THROUGH as err:
                    self.requests += 1
                    self._trace_rejections(messages, step, model_id)
                    wait = _minute_quota_wait(err)
                    if wait is not None and attempt < self.settings.max_attempts:
                        # A per-minute limit passes by itself; a daily one does not.
                        self.trace.event("llm_retry", step=step, model=model_id,
                                         attempt=attempt, error="minute_quota", delay=wait)
                        await self._sleep(wait)
                        continue
                    if isinstance(err, pai.ModelQuotaExceededError | pai.ModelAuthenticationError):
                        # No point asking this model again in this run.
                        reason = ("квота или баланс исчерпаны"
                                  if isinstance(err, pai.ModelQuotaExceededError)
                                  else "ключ отклонён провайдером")
                        self._unavailable[model_id] = reason
                        self.trace.event("llm_model_disabled", step=step, model=model_id,
                                         reason=reason, detail=str(err)[:500])
                        raise _ModelGaveUp(f"{reason} ({err})") from err
                    raise _ModelGaveUp(f"{type(err).__name__}: {err}") from err
            usage = result.usage
            self.requests += max(usage.requests, 1)
            return result.output, usage, attempt, time.perf_counter() - started

    async def _backoff(self, err: Exception, attempt: int, step: str, model_id: str) -> None:
        delay = getattr(err, "retry_after", None)
        if delay is None:
            delay = self.settings.backoff_base_s * 2 ** (attempt - 1) * (1 + random.random() / 4)
        self.trace.event("llm_retry", step=step, model=model_id, attempt=attempt,
                         error=type(err).__name__, delay=round(delay, 2))
        await self._sleep(delay)

    def _trace_rejections(self, messages: list, step: str, model_id: str) -> None:
        """Record why the model's answers were rejected; otherwise the cause is invisible.

        Schema errors come back to the model as the output tool's return value, checks as
        retry prompts; both are collected.
        """
        feedback = []
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, RetryPromptPart):
                    feedback.append(part.model_response()[:1500])
                elif isinstance(part, ToolReturnPart):
                    feedback.append(str(part.content)[:1500])
        if feedback:
            self.trace.event("llm_invalid_output", step=step, model=model_id, feedback=feedback)


_MINUTE_QUOTA = re.compile(r"PerMinute", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")
_MAX_QUOTA_WAIT_S = 120.0


def _minute_quota_wait(err: Exception) -> float | None:
    """Seconds to wait out a per-minute quota (Gemini free tier), or None if it is not one."""
    if not isinstance(err, pai.ModelQuotaExceededError):
        return None
    text = str(err)
    if not _MINUTE_QUOTA.search(text) or "PerDay" in text:
        return None
    match = _RETRY_DELAY.search(text)
    delay = float(match.group(1)) + 1.0 if match else 30.0
    return delay if delay <= _MAX_QUOTA_WAIT_S else None


_WARM_UP_MIN_GROUP = 3
_PREFIX_CHARS = 4000


def _prefix_key(request: dict[str, Any]) -> str:
    """Requests with equal keys share instructions and the start of the prompt."""
    head = request["instructions"] + "\0" + request["prompt"][:_PREFIX_CHARS]
    return "reqaudit-" + hashlib.sha256(head.encode("utf-8")).hexdigest()[:24]


class _ModelGaveUp(Exception):
    """This model cannot answer right now; try the next one in the chain."""
