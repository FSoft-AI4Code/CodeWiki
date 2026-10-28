"""Request-level API accounting, shared with nested agents via context variables."""

from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai.models.wrapper import WrapperModel

from codewiki.src.be.backend import UsageLedger, usage_to_dict
from codewiki.src.be.pricing import price_usage


def _value(obj: Any, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def normalize_api_usage(usage: Any) -> dict[str, Any] | None:
    """Normalize OpenAI aliases and cache details to inclusive input counts.

    pydantic-ai and Google's adapter already provide inclusive input. OpenAI
    prompt_tokens also includes cache reads. Never sum aliases or cache subsets.
    """
    data = usage_to_dict(usage)
    if not data or not any(
        k in data for k in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens")
    ):
        return None
    details = _value(usage, "prompt_tokens_details")
    out = {
        "input_tokens": data.get("input_tokens", data.get("prompt_tokens", 0)),
        "output_tokens": data.get("output_tokens", data.get("completion_tokens", 0)),
        "cache_read_tokens": data.get(
            "cache_read_tokens", _value(details, "cached_tokens", 0) or 0
        ),
        "cache_write_tokens": data.get("cache_write_tokens", 0),
        "requests": 1,
    }
    out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out


@dataclass
class UsageContext:
    ledger: UsageLedger
    config: Any
    kind: str
    target: str
    entries: list[dict[str, Any]] = field(default_factory=list)

    def totals(self) -> dict[str, Any] | None:
        totals: dict[str, Any] = {}
        for entry in self.entries:
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
                "requests",
                "cost_usd",
            ):
                if key in entry:
                    totals[key] = totals.get(key, 0) + entry[key]
        return totals or None


_CONTEXT: ContextVar[UsageContext | None] = ContextVar("codewiki_api_usage", default=None)


@contextlib.contextmanager
def track_usage(ledger: UsageLedger, config: Any, kind: str, target: str = ""):
    context = UsageContext(ledger, config, kind, target)
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


@contextlib.contextmanager
def usage_target(target: str):
    """Nested agents share accounting but label their own page."""
    parent = _CONTEXT.get()
    token = _CONTEXT.set(replace(parent, target=target) if parent else None)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def record_response(
    usage: Any,
    requested_model: str,
    *,
    model: str | None = None,
    seconds: float = 0.0,
    error: str | None = None,
) -> dict[str, Any] | None:
    context = _CONTEXT.get()
    if context is None:
        return normalize_api_usage(usage)
    normalized = normalize_api_usage(usage)
    actual_model = model or requested_model
    priced = price_usage(
        normalized,
        getattr(context.config, "pricing", {}),
        context.config.provider,
        actual_model,
        requested_model,
    )
    entry = context.ledger.add(
        context.kind,
        context.target,
        normalized,
        model=actual_model,
        seconds=seconds,
        provider=context.config.provider,
        requested_model=requested_model,
        **priced,
        **({"error": error} if error else {}),
    )
    context.entries.append(entry)
    return {
        **(normalized or {}),
        **({"cost_usd": priced["cost_usd"]} if "cost_usd" in priced else {}),
    } or None


class UsageModel(WrapperModel):
    """Observe each model before FallbackModel can combine its results."""

    def __init__(self, wrapped, requested_model: str):
        super().__init__(wrapped)
        self.requested_model = requested_model

    async def request(self, messages, model_settings, model_request_parameters):
        started = time.monotonic()
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except Exception as exc:
            # Save the type only: provider exceptions can contain credentials and prompts.
            record_response(
                None,
                self.requested_model,
                seconds=time.monotonic() - started,
                error=type(exc).__name__,
            )
            raise
        record_response(
            response.usage,
            self.requested_model,
            model=response.model_name,
            seconds=time.monotonic() - started,
        )
        return response
