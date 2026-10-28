"""User-maintained API prices, in USD per million tokens."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any


def validate_pricing(pricing: dict) -> None:
    """Reject malformed persisted prices as well as invalid CLI inputs."""
    if not isinstance(pricing, dict):
        raise ValueError("pricing must be a provider/model mapping")
    for provider, models in pricing.items():
        if not isinstance(provider, str) or not provider or not isinstance(models, dict):
            raise ValueError("pricing must be a provider/model mapping")
        for model, rates in models.items():
            if not isinstance(model, str) or not model or not isinstance(rates, dict):
                raise ValueError("pricing models must have rate mappings")
            if not {"input", "output"} <= rates.keys():
                raise ValueError("pricing requires input and output rates")
            for key, rate in rates.items():
                if key not in {"input", "output", "cache_read", "cache_write"}:
                    raise ValueError(f"Unknown pricing rate: {key}")
                if (
                    isinstance(rate, bool)
                    or not isinstance(rate, (int, float))
                    or not math.isfinite(rate)
                    or rate < 0
                ):
                    raise ValueError("Pricing rates must be finite, nonnegative numbers")


def price_usage(
    usage: dict[str, Any] | None,
    pricing: dict,
    provider: str,
    model: str,
    requested_model: str,
) -> dict[str, Any]:
    """Price inclusive input tokens; unknown usage/prices never mean free."""
    models = pricing.get(provider, {})
    pricing_model = model if model in models else requested_model
    configured = models.get(pricing_model)
    if usage is None or configured is None:
        return {"cost_source": "unknown"}
    rates = dict(configured)
    rates.setdefault("cache_read", rates["input"])
    rates.setdefault("cache_write", rates["input"])
    read = usage.get("cache_read_tokens", 0)
    write = usage.get("cache_write_tokens", 0)
    uncached = usage.get("input_tokens", 0) - read - write
    if uncached < 0:
        return {"cost_source": "unknown"}
    buckets = {
        "input": uncached,
        "output": usage.get("output_tokens", 0),
        "cache_read": read,
        "cache_write": write,
    }
    cost = sum(Decimal(str(rates[k])) * Decimal(str(n)) for k, n in buckets.items())
    return {
        "cost_usd": float(cost / 1_000_000),
        "cost_source": "configured",
        "pricing_model": pricing_model,
        "rates": rates,
    }
