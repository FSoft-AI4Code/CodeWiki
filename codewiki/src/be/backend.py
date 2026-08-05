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
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from codewiki.src.be.dependency_analyzer.models.core import Node


CAW_PROVIDERS = frozenset({"claude-code", "codex"})


def is_caw_provider(provider: str) -> bool:
    """Return True if *provider* uses caw (CLI subscription mode)."""
    return provider in CAW_PROVIDERS


class LLMBackend(abc.ABC):
    """Abstract LLM backend used by the documentation generator."""

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


def get_backend(config) -> "LLMBackend":
    """Return the backend instance matching ``config.provider``."""
    provider = getattr(config, "provider", "openai-compatible")
    if is_caw_provider(provider):
        from codewiki.src.be.caw_backend import CawBackend
        return CawBackend(config)
    from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend
    return PydanticAIBackend(config)
