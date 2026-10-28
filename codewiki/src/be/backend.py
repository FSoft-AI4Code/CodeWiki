"""LLMBackend — unified abstraction over the API and subscription LLM paths.

CodeWiki has two LLM call shapes:

* a synchronous single-shot completion (clustering, parent / repo overviews)
* an asynchronous multi-turn agentic loop with custom tools (per-module docs)

Two implementations satisfy this interface:

* :class:`PydanticAIBackend` — wraps the existing openai-compatible / anthropic
  / bedrock / azure-openai paths via pydantic-ai + litellm, and the native
  Google paths (``google``, ``google-vertex``) via pydantic-ai + google-genai.
  API-key based, except Vertex AI which uses Google credentials.
* :class:`CawBackend` — routes through the ``claude`` or ``codex`` CLI via the
  :mod:`caw` library, using the user's OAuth subscription.  No API key.

Provider selection happens in one place: :func:`get_backend`.
"""

from __future__ import annotations

import abc
import contextlib
import threading
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from codewiki.src.be.agent_tools.deps import CodeWikiDeps
    from codewiki.src.be.dependency_analyzer.models.core import Node


@dataclass
class AgentReply:
    """Result of one agentic run whose final message matters (the updater)."""

    text: str
    usage: dict[str, Any] | None = None
    seconds: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


def usage_to_dict(usage: Any) -> dict[str, Any] | None:
    """Best-effort conversion of a provider usage object to plain numbers."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return {
            k: v
            for k, v in usage.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        } or None
    out: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "requests",
        "cost_usd",
    ):
        value = getattr(usage, key, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    if not out and hasattr(usage, "model_dump"):
        try:
            return {k: v for k, v in usage.model_dump().items() if isinstance(v, (int, float))}
        except Exception:  # noqa: BLE001 — usage is optional telemetry
            return None
    return out or None


_TOKEN_KEYS = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")


@dataclass
class UsageLedger:
    """Every LLM call of one CodeWiki run, with its tokens and cost.

    Calls are recorded once per CLI process / agent run, so nested caw
    sub-module agents (separate ``claude`` / ``codex`` processes) add up
    without double counting.  ``add`` can be called from caw's MCP worker
    threads, hence the lock.  ``phase`` labels the stage the calls belong to
    (clustering, modules, overview, ...) and ``target`` the page a
    ``complete()`` call is for; stages run sequentially.
    """

    phase: str = "generate"
    target: str = ""
    entries: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @contextlib.contextmanager
    def in_phase(self, name: str, target: str = "") -> Iterator[None]:
        previous = (self.phase, self.target)
        self.phase, self.target = name, target
        try:
            yield
        finally:
            self.phase, self.target = previous

    def add(
        self,
        kind: str,
        target: str,
        usage: dict[str, Any] | None,
        *,
        seconds: float = 0.0,
        model: str | None = None,
        **metadata: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "phase": self.phase,
            "kind": kind,
            "target": target or self.target,
            "model": model or "",
            "seconds": round(seconds, 2),
            **(usage or {}),
            "usage_reported": usage is not None,
            **metadata,
        }
        with self._lock:
            self.entries.append(entry)
        return entry

    def summary(self) -> dict[str, Any]:
        """One accounting result, grouped for display without charging twice."""
        with self._lock:
            entries = deepcopy(self.entries)
        totals: dict[str, float] = {}
        by_phase: dict[str, dict[str, float]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        unpriced: set[str] = set()
        priced_calls = 0
        unknown_usage = 0
        for e in entries:
            phase_totals = by_phase.setdefault(e["phase"], {"calls": 0})
            model_totals = by_model.setdefault(e["model"] or "<default>", {"calls": 0})
            phase_totals["calls"] += 1
            model_totals["calls"] += 1
            for k, v in e.items():
                if k not in (
                    *_TOKEN_KEYS,
                    "total_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "requests",
                    "cost_usd",
                ):
                    continue
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                totals[k] = totals.get(k, 0) + v
                phase_totals[k] = phase_totals.get(k, 0) + v
                model_totals[k] = model_totals.get(k, 0) + v
            # Legacy integrations use zero for "no rate"; explicitly priced zero is valid.
            priced = e.get("cost_usd") is not None and (
                e.get("cost_usd") != 0
                or e.get("cost_source") in ("configured", "provider")
                or not any(e.get(k) for k in _TOKEN_KEYS)
            )
            if priced:
                priced_calls += 1
            else:
                unpriced.add(e["model"] or "<default>")
                for group in (phase_totals, model_totals):
                    group["unpriced_calls"] = group.get("unpriced_calls", 0) + 1
            if not e.get("usage_reported", True):
                unknown_usage += 1
        if not entries:
            totals["cost_usd"] = 0.0
        status = (
            "complete"
            if priced_calls == len(entries)
            else ("partial" if priced_calls else "unknown")
        )
        return {
            "schema_version": 2,
            "scope": "this run only (modules resumed from disk cost nothing)",
            "currency": "USD",
            "cost_status": status,
            "incomplete": self.incomplete,
            "unknown_usage_calls": unknown_usage,
            "n_calls": len(entries),
            "totals": totals,
            "by_phase": by_phase,
            "by_model": by_model,
            "unpriced_models": sorted(unpriced),
            "calls": entries,
        }


CAW_PROVIDERS = frozenset({"claude-code", "codex"})


def is_caw_provider(provider: str) -> bool:
    """Return True if *provider* uses caw (CLI subscription mode)."""
    return provider in CAW_PROVIDERS


GOOGLE_PROVIDERS = frozenset({"google", "google-vertex"})


def is_google_provider(provider: str) -> bool:
    """Return True if *provider* talks to Gemini natively (Gemini API or Vertex AI)."""
    return provider in GOOGLE_PROVIDERS


def needs_api_key(provider: str) -> bool:
    """Return True if *provider* authenticates with a stored API key.

    caw providers use the CLI's OAuth login, google-vertex uses Google
    credentials (ADC / Workload Identity Federation).
    """
    return not is_caw_provider(provider) and provider != "google-vertex"


def needs_base_url(provider: str) -> bool:
    """Return True if *provider* needs a configured base URL."""
    return not is_caw_provider(provider) and not is_google_provider(provider)


class LLMBackend(abc.ABC):
    """Abstract LLM backend used by the documentation generator.

    ``last_usage`` holds the token usage of the most recent ``complete`` /
    ``run_module_agent`` / ``run_update_agent`` call when the provider exposes
    it (``None`` otherwise). The incremental updater reads it for its record.
    ``usage_ledger`` accumulates every call of the run for the cost report.
    """

    last_usage: dict[str, Any] | None = None
    _usage_ledger: UsageLedger | None = None

    @property
    def usage_ledger(self) -> UsageLedger:
        if self._usage_ledger is None:
            self._usage_ledger = UsageLedger()
        return self._usage_ledger

    def _record_usage(
        self,
        kind: str,
        target: str,
        usage: Any,
        *,
        seconds: float = 0.0,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        """Set ``last_usage`` from a provider usage object and log it in the ledger."""
        usage_dict = usage_to_dict(usage)
        provider = getattr(getattr(self, "_config", None), "provider", "")
        if not provider:
            provider = {"claude_code": "claude-code"}.get(
                getattr(self, "_caw_provider", ""), getattr(self, "_caw_provider", "")
            )
        if usage_dict is not None and is_caw_provider(provider):
            # caw exposes uncached input for both CLI providers.
            usage_dict["input_tokens"] = sum(
                usage_dict.get(k, 0)
                for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
            )
            usage_dict["total_tokens"] = usage_dict["input_tokens"] + usage_dict.get(
                "output_tokens", 0
            )
        self.last_usage = usage_dict
        source = "provider" if usage_dict and usage_dict.get("cost_usd") else "unknown"
        self.usage_ledger.add(
            kind,
            target,
            usage_dict,
            seconds=seconds,
            model=model,
            provider=provider,
            cost_source=source,
        )
        return usage_dict

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Single-shot text completion, with an optional system message."""

    @abc.abstractmethod
    async def run_module_agent(
        self,
        module_name: str,
        components: Dict[str, "Node"],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
    ) -> Dict[str, Any]:
        """Run the per-module agent loop.  Returns the updated module_tree dict."""

    async def run_update_agent(
        self,
        system_prompt: str,
        user_prompt: str,
        deps: "CodeWikiDeps",
    ) -> AgentReply:
        """Run an editing agent (read + str_replace_editor tools, no delegation)
        and return its final message. Used by the incremental updater; writes
        are limited by ``deps.allowed_write_paths``."""
        raise NotImplementedError(f"{type(self).__name__} does not support update agents")


def get_backend(config) -> "LLMBackend":
    """Return the backend instance matching ``config.provider``."""
    provider = getattr(config, "provider", "openai-compatible")
    if is_caw_provider(provider):
        from codewiki.src.be.caw_backend import CawBackend

        return CawBackend(config)
    from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend

    return PydanticAIBackend(config)
