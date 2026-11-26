"""Cost of model calls from the genai-prices catalogue (the one PydanticAI uses).

Prices are an estimate: the catalogue may lag behind the provider, and a model it does not
know has no price. The passport says so instead of showing zero.
"""

from __future__ import annotations

import warnings

from genai_prices import Usage, calc_price

_PROVIDER = {"openai": "openai", "gemini": "google", "google": "google", "vertex": "google",
             "anthropic": "anthropic"}


def price_usd(model_id: str, input_tokens: int, cached_tokens: int,
              output_tokens: int) -> float | None:
    """Price of one call in USD, or None if the model is not in the catalogue.

    `input_tokens` includes the cached ones, as providers report them.
    """
    provider, _, name = model_id.partition(":")
    usage = Usage(input_tokens=input_tokens, cache_read_tokens=cached_tokens,
                  output_tokens=output_tokens)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return float(calc_price(usage, model_ref=name,
                                    provider_id=_PROVIDER.get(provider, provider)).total_price)
    except Exception:  # noqa: BLE001 - an unknown model just has no price
        return None
