"""Compatibility smoke tests for the pinned "latest" dependency stack.

The repo deliberately tracks the newest versions of its core LLM/MCP
libraries (pydantic-ai, openai, litellm, mcp, griffe).  These libraries
have frequent breaking changes, so this module pins the *contracts* the
codebase relies on as executable tests:

* key top-level modules import cleanly,
* ``Config`` -> ``create_fallback_models`` -> ``PydanticAIBackend``
  wires up without network access,
* the ``Agent(...)`` construction signature used by the backend is valid
  against the installed pydantic-ai (positional model, ``name``,
  ``deps_type``, ``tools``, ``system_prompt``),
* the mcp server still finds the 1.x symbols it imports
  (``mcp.server.stdio``, ``mcp.server.fastmcp.Context``).

All checks are offline: no LLM call is made and no API key is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from codewiki.src.be.llm_services import create_fallback_models
from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend
from codewiki.src.config import Config

pydantic_ai = pytest.importorskip("pydantic_ai")
Agent = pytest.importorskip("pydantic_ai.agent").Agent
FallbackModel = pytest.importorskip("pydantic_ai.models.fallback").FallbackModel


def _make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "repo_path": "/tmp/does-not-exist",
        "output_dir": "/tmp/out",
        "dependency_graph_dir": "/tmp/out/dep-graph",
        "docs_dir": "/tmp/out/docs",
        "max_depth": 5,
        "llm_base_url": "http://localhost:9/v1",
        "llm_api_key": "test-key",
        "main_model": "test/main-model",
        "cluster_model": "test/cluster-model",
    }
    defaults.update(overrides)
    return Config(**defaults)


def test_core_modules_import() -> None:
    import codewiki.mcp.server
    import codewiki.src.be.agent_tools.generate_sub_module_documentations
    import codewiki.src.be.agent_tools.read_code_components
    import codewiki.src.be.agent_tools.str_replace_editor
    import codewiki.src.be.caw_toolkit
    import codewiki.src.be.llm_services
    import codewiki.src.be.pydantic_ai_backend  # noqa: F401


def test_griffe_docstring_symbols() -> None:
    from griffe import (  # noqa: F401
        Docstring,
        DocstringSectionKind,
        GoogleOptions,
    )


def test_mcp_server_1x_symbols() -> None:
    from mcp.server import Server  # noqa: F401
    from mcp.server.fastmcp import Context  # noqa: F401
    from mcp.server.stdio import stdio_server  # noqa: F401
    from mcp.types import TextContent, Tool  # noqa: F401


def test_openai_chat_types() -> None:
    from openai.types.chat import (  # noqa: F401
        ChatCompletion,
        ChatCompletionMessage,
        ChatCompletionMessageParam,
    )


def test_fallback_models_wiring() -> None:
    config = _make_config()
    fallback = create_fallback_models(config)
    assert isinstance(fallback, FallbackModel)
    assert len(fallback.models) == 2


def test_backend_instantiation() -> None:
    backend = PydanticAIBackend(_make_config())
    assert backend._config is not None


def test_agent_construction_signature() -> None:
    config = _make_config()
    fallback = create_fallback_models(config)

    class DummyDeps:
        pass

    agent = Agent(
        fallback,
        name="test-module",
        deps_type=DummyDeps,
        tools=[
            lambda: None,  # zero-arg tool
        ],
        system_prompt="You are a test agent.",
    )
    assert agent.name == "test-module"
    assert agent.deps_type is DummyDeps

    # Generated tool objects must expose the fields the backend relies on.
    generated = agent._function_toolset.tools
    assert generated, "expected at least one tool on the agent"
    for tool in generated.values():
        assert isinstance(tool.name, str) and tool.name
        assert hasattr(tool, "takes_ctx")
        assert callable(tool.function)
