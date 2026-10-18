"""Checks for behaviour that Cython compilation can break.

These run on source in CI and, through packaging/build/test_wheel.sh, against
every compiled wheel on Linux, Windows and macOS. They cover what the rest of
the suite does not reach: every module importing, the introspection that
pydantic-ai, FastMCP and FastAPI do on compiled functions, and a module agent
run end to end with a scripted model, so no API key or network is needed.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest

import codewiki

# ---------------------------------------------------------------------------
# Every module imports
# ---------------------------------------------------------------------------


def test_every_module_imports():
    failures = {}
    count = 0
    for info in pkgutil.walk_packages(codewiki.__path__, "codewiki."):
        count += 1
        try:
            importlib.import_module(info.name)
        except Exception as e:  # noqa: BLE001 — collect every failure, not just the first
            failures[info.name] = f"{type(e).__name__}: {e}"
    assert count > 100, f"only {count} modules found; is the package complete?"
    assert not failures, failures


# ---------------------------------------------------------------------------
# Introspection by frameworks
# ---------------------------------------------------------------------------


# caw's toolkit still uses asyncio.iscoroutinefunction, so check both forms.
@pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction' is deprecated")
def test_async_functions_are_still_coroutine_functions():
    # pydantic-ai, FastMCP and caw pick sync vs async by inspecting these;
    # a compiled function that stops reporting as a coroutine function
    # would have its coroutine returned unawaited.
    from codewiki.src.be.caw_toolkit import CawToolKit
    from codewiki.src.be.documentation_generator import DocumentationGenerator
    from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend

    for fn in (
        PydanticAIBackend.run_module_agent,
        DocumentationGenerator.run,
        CawToolKit.read_code_components,
        CawToolKit.str_replace_editor,
        CawToolKit.generate_sub_module_documentation,
    ):
        assert inspect.iscoroutinefunction(fn), fn.__qualname__
        assert asyncio.iscoroutinefunction(fn), fn.__qualname__


def test_pydantic_ai_tool_schemas():
    from codewiki.src.be.agent_tools.generate_sub_module_documentations import (
        generate_sub_module_documentation_tool,
    )
    from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool

    expected = {
        "read_code_components": {"component_ids"},
        "str_replace_editor": {"working_dir", "command", "path", "file_text"},
        "generate_sub_module_documentation": {"sub_module_specs"},
    }
    for tool in (
        read_code_components_tool,
        str_replace_editor_tool,
        generate_sub_module_documentation_tool,
    ):
        schema = tool.function_schema
        assert tool.takes_ctx, tool.name
        assert schema.is_async, tool.name
        assert tool.description and tool.description.strip(), tool.name
        properties = set(schema.json_schema.get("properties", {}))
        assert expected[tool.name] <= properties, (tool.name, properties)
        # The parameter docs come from the docstring; compiled code must keep it.
        if tool.name == "str_replace_editor":
            assert "description" in schema.json_schema["properties"]["working_dir"]


def test_caw_toolkit_tools_register_with_fastmcp():
    from mcp.server.fastmcp import FastMCP

    from codewiki._vendor.caw.mcp import register_tool
    from codewiki._vendor.caw.toolkit import _make_tool_func
    from codewiki.src.be.caw_toolkit import CawToolKit

    server = FastMCP("compiled-wheel-check")
    for name in dir(CawToolKit):
        attr = getattr(CawToolKit, name)
        info = getattr(attr, "_toolkit_tool_info", None)
        if info is not None:
            register_tool(server, _make_tool_func(attr, info))

    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {
        "read_code_components",
        "str_replace_editor",
        "generate_sub_module_documentation",
    } <= set(tools)
    editor = tools["str_replace_editor"]
    assert editor.description
    assert {"working_dir", "command", "path"} <= set(editor.inputSchema["properties"])
    assert {"working_dir", "command"} <= set(editor.inputSchema.get("required", []))
    # The Context parameter is injected by FastMCP, never exposed to the model.
    assert "ctx" not in editor.inputSchema["properties"]


@pytest.mark.parametrize("module", ["codewiki.src.fe.web_app", "codewiki.src.fe.visualise_docs"])
def test_fastapi_routes_build_an_openapi_schema(module):
    app = importlib.import_module(module).app
    schema = app.openapi()
    assert schema["paths"], module


# ---------------------------------------------------------------------------
# End to end: dependency analysis + module agent with a scripted model
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_module_agent_end_to_end_with_scripted_model(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    import codewiki.src.be.pydantic_ai_backend as backend_module
    from codewiki.src.be.dependency_analyzer.dependency_graphs_builder import (
        DependencyGraphBuilder,
    )
    from codewiki.src.config import MODULE_TREE_FILENAME, Config

    repo = tmp_path / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "greeter.py",
        "from collections import defaultdict\n\n"
        "from pkg.format import shout\n\n\n"
        "class Greeter:\n"
        "    def __init__(self):\n"
        "        self.seen = defaultdict(int)\n\n"
        "    def greet(self, name):\n"
        "        self.seen[name] += 1\n"
        "        return shout(name)\n",
    )
    _write(repo / "pkg" / "format.py", "def shout(name):\n    return f'HELLO {name.upper()}'\n")
    out = tmp_path / "docs"
    out.mkdir()

    config = Config.from_cli(
        repo_path=str(repo),
        output_dir=str(out),
        llm_base_url="http://127.0.0.1:9",
        llm_api_key="unused",
        main_model="scripted",
        cluster_model="scripted",
    )

    # Dependency analysis: the compiled-wheel crash (defaultdict vs a `dict`
    # annotation) happened in this step.
    components, leaves = DependencyGraphBuilder(config).build_dependency_graph()
    ids = set(components)
    assert any(i.endswith("::Greeter") for i in ids), ids
    assert any(i.endswith("::shout") for i in ids), ids
    assert leaves

    (out / MODULE_TREE_FILENAME).write_text(
        json.dumps({"Core": {"path": "pkg", "components": leaves, "children": {}}}),
        encoding="utf-8",
    )

    tool_returns: list[str] = []

    def scripted(messages, info):
        tool_returns[:] = [
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        step = len(tool_returns)
        if step == 0:
            return ModelResponse(
                parts=[ToolCallPart("read_code_components", {"component_ids": leaves})]
            )
        if step == 1:
            page = (
                "# Core\n\nGreets people.\n\n```mermaid\ngraph TD\n  A[Greeter] --> B[shout]\n```\n"
            )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "str_replace_editor",
                        {
                            "working_dir": "docs",
                            "command": "create",
                            "path": "Core.md",
                            "file_text": page,
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Documentation written.")])

    monkeypatch.setattr(
        backend_module, "create_fallback_models", lambda config: FunctionModel(scripted)
    )
    backend = backend_module.PydanticAIBackend(config)
    asyncio.run(backend.run_module_agent("Core", components, leaves, ["Core"], str(out)))

    assert len(tool_returns) == 2, tool_returns
    read_result, editor_result = tool_returns
    assert "class Greeter" in read_result or "def shout" in read_result, read_result[:300]
    assert "created" in editor_result.lower(), editor_result[:300]
    page = (out / "Core.md").read_text(encoding="utf-8")
    assert page.startswith("# Core")
    assert "```mermaid" in page
