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


CAW_PROVIDERS = frozenset({"claude-code", "codex"})


def is_caw_provider(provider: str) -> bool:
    """Return True if *provider* uses caw (CLI subscription mode)."""
    return provider in CAW_PROVIDERS


class LLMBackend(abc.ABC):
    """Abstract LLM backend used by the documentation generator.

    ``last_usage`` holds the token usage of the most recent ``complete`` /
    ``run_module_agent`` / ``run_update_agent`` call when the provider exposes
    it (``None`` otherwise). The incremental updater reads it for its record.
    """

    last_usage: dict[str, Any] | None = None

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
    ) -> str:
        """Single-shot text completion."""

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
