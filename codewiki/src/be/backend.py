"""LLMBackend — unified abstraction over the API and subscription LLM paths.

CodeWiki has two LLM call shapes:

* a synchronous single-shot completion (clustering, parent / repo overviews)
* an asynchronous multi-turn agentic loop with custom tools (per-module docs)

Two implementations satisfy this interface:

* :class:`PydanticAIBackend` — wraps the existing openai-compatible / anthropic
  / bedrock / azure-openai paths via pydantic-ai + litellm.  API-key based.
* :class:`CawBackend` — routes through the ``claude`` or ``codex`` CLI via the
  :mod:`caw` library, using the user's OAuth subscription.  No API key.

Provider selection happens in one place: :func:`get_backend`.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import contextvars
import functools
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List

from codewiki.src.be.agent_slots import AgentSlots, SharedModuleTree

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
        return {k: v for k, v in usage.items() if isinstance(v, (int, float))} or None
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


# (phase, target) of the asyncio task / thread recording a call; see UsageLedger.
_PHASE: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "codewiki_usage_phase", default=None
)


@dataclass
class UsageLedger:
    """Every LLM call of one CodeWiki run, with its tokens and cost.

    Calls are recorded once per CLI process / agent run, so nested caw
    sub-module agents (separate ``claude`` / ``codex`` processes) add up
    without double counting.  ``add`` can be called from caw's MCP worker
    threads, hence the lock.  ``phase`` labels the stage the calls belong to
    (clustering, modules, overview, ...) and ``target`` the page a
    ``complete()`` call is for.

    Module agents run concurrently, so a phase is kept per asyncio task
    (``_PHASE``, which ``asyncio.to_thread`` carries into its thread): one
    parent page's phase never relabels another agent's calls. Stage-wide
    phases (no target) also set ``phase`` on the ledger, the fallback for
    threads that do not inherit the context, such as caw's MCP workers.
    """

    phase: str = "generate"
    target: str = ""
    entries: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @contextlib.contextmanager
    def in_phase(self, name: str, target: str = "") -> Iterator[None]:
        token = _PHASE.set((name, target))
        previous = (self.phase, self.target)
        if not target:
            self.phase, self.target = name, ""
        try:
            yield
        finally:
            _PHASE.reset(token)
            if not target:
                self.phase, self.target = previous

    def add(
        self,
        kind: str,
        target: str,
        usage: dict[str, Any] | None,
        *,
        seconds: float = 0.0,
        model: str | None = None,
    ) -> None:
        phase, phase_target = _PHASE.get() or (self.phase, self.target)
        entry: dict[str, Any] = {
            "phase": phase,
            "kind": kind,
            "target": target or phase_target,
            "model": model or "",
            "seconds": round(seconds, 2),
            **(usage or {}),
        }
        with self._lock:
            self.entries.append(entry)

    def summary(self) -> dict[str, Any]:
        """Totals overall and per phase, plus the per-call list.

        A call with tokens but no ``cost_usd`` is *unpriced*: its model is
        listed under ``unpriced_models`` so a $0 total is never mistaken for
        a free run.
        """
        with self._lock:
            entries = [dict(e) for e in self.entries]
        totals: dict[str, float] = {}
        by_phase: dict[str, dict[str, float]] = {}
        unpriced: set[str] = set()
        for e in entries:
            phase_totals = by_phase.setdefault(e["phase"], {"calls": 0})
            phase_totals["calls"] += 1
            for k, v in e.items():
                if k == "seconds" or not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                totals[k] = totals.get(k, 0) + v
                phase_totals[k] = phase_totals.get(k, 0) + v
            if not e.get("cost_usd") and any(e.get(k) for k in _TOKEN_KEYS):
                unpriced.add(e["model"] or "<default>")
        return {
            "scope": "this run only (modules resumed from disk cost nothing)",
            "n_calls": len(entries),
            "totals": totals,
            "by_phase": by_phase,
            "unpriced_models": sorted(unpriced),
            "calls": entries,
        }


CAW_PROVIDERS = frozenset({"claude-code", "codex"})


def is_caw_provider(provider: str) -> bool:
    """Return True if *provider* uses caw (CLI subscription mode)."""
    return provider in CAW_PROVIDERS


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
        self.last_usage = usage_dict
        self.usage_ledger.add(kind, target, usage_dict, seconds=seconds, model=model)
        return usage_dict

    @functools.cached_property
    def slots(self) -> AgentSlots:
        """Caps concurrent agent sessions at ``config.concurrency``."""
        return AgentSlots(getattr(getattr(self, "_config", None), "concurrency", 1))

    @functools.cached_property
    def module_tree(self) -> SharedModuleTree:
        """The module tree shared by every agent of this run."""
        return SharedModuleTree()

    async def complete_async(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """``complete`` in a worker thread, inside a slot, so it does not block
        the event loop (and the agents' Mermaid validation that runs on it)."""
        async with self.slots.hold_async():
            return await asyncio.to_thread(
                self.complete, prompt, model=model, system_prompt=system_prompt
            )

    @abc.abstractmethod
    async def run_overview_agent(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        working_dir: str,
        target: str,
    ) -> str:
        """Generate a parent or repository overview.

        ``system_prompt`` is the shared custom-instructions block from
        :func:`~codewiki.src.be.prompt_template.format_overview_system_prompt`;
        each backend appends its own tool guidance before calling the model.
        """

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        allow_tools: bool = True,
    ) -> str:
        """Single-shot text completion, with an optional system message.

        ``allow_tools=False`` asks for a plain answer from the prompt alone:
        a subscription CLI would otherwise open files itself (the Q&A engine
        already put exactly what is needed in the prompt).
        """

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
