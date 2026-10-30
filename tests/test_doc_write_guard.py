"""An agent that ends without writing its page is reminded to write it (issue #134)."""

import asyncio
import logging
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.doc_guard import (
    DOC_WRITE_REMINDERS,
    DocumentationWriteError,
    doc_written,
    run_until_doc_written,
)
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool


def _deps(tmp_path, module_tree=None):
    return CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components={},
        path_to_current_module=[],
        current_module_name="repo",
        module_tree=module_tree or {},
        max_depth=2,
        current_depth=1,
        config=SimpleNamespace(),
    )


def _create(path):
    return ToolCallPart(
        "str_replace_editor",
        {"working_dir": "docs", "command": "create", "path": path, "file_text": "# Page\n"},
    )


def _scripted_agent(responses):
    """Agent whose model replies with ``responses`` in order, one per request."""
    calls = []

    def model(messages, info: AgentInfo):
        # Older PydanticAI versions extend the same list after the callback.
        calls.append(deepcopy(messages))
        return ModelResponse(parts=responses[len(calls) - 1])

    agent = Agent(FunctionModel(model), deps_type=CodeWikiDeps, tools=[str_replace_editor_tool])
    return agent, calls


def _run(agent, deps, module_name, module_path):
    return asyncio.run(
        run_until_doc_written(
            agent,
            "document it",
            deps=deps,
            module_name=module_name,
            module_path=module_path,
            layout="flat",
        )
    )


def test_agent_reminded_until_page_written(tmp_path, caplog):
    agent, calls = _scripted_agent(
        [
            [TextPart("Here is the documentation: ...")],
            [_create("./repo.md")],
            [TextPart("done")],
        ]
    )
    with caplog.at_level(logging.WARNING):
        result, usage = _run(agent, _deps(tmp_path), "repo", [])

    assert Path(tmp_path, "repo.md").read_text() == "# Page\n"
    assert result.output == "done"
    assert usage.requests == 3
    # The reminder continues the same conversation and names the exact file.
    assert len(calls[1]) == 3
    assert "./repo.md" in calls[1][-1].parts[-1].content
    assert "finished without writing repo.md" in caplog.text
    assert "Here is the documentation" in caplog.text
    assert "editor writes: none" in caplog.text


def test_written_page_needs_no_reminder(tmp_path):
    agent, calls = _scripted_agent([[_create("./billing.md")], [TextPart("done")]])
    deps = _deps(tmp_path, {"billing": {"components": [], "children": {}}})
    _run(agent, deps, "billing", ["billing"])
    assert len(calls) == 2


def test_gives_up_after_reminders_and_logs_tool_errors(tmp_path, caplog):
    # The model keeps writing to the repo, which the tool rejects.
    bad_write = [
        ToolCallPart(
            "str_replace_editor",
            {"working_dir": "repo", "command": "create", "path": "x.md", "file_text": "#"},
        )
    ]
    responses = []
    for _ in range(DOC_WRITE_REMINDERS + 1):
        responses += [bad_write, [TextPart("I wrote it")]]
    agent, calls = _scripted_agent(responses)
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(DocumentationWriteError, match="2 reminders"),
    ):
        _run(agent, _deps(tmp_path), "repo", [])

    assert len(calls) == 2 * (DOC_WRITE_REMINDERS + 1)
    assert not doc_written(str(tmp_path), "repo", [], {})
    assert "still has not written repo.md" in caplog.text
    assert "The `view` command is the only allowed command" in caplog.text


def test_warning_reports_rejected_tool_calls(tmp_path, caplog):
    # A call missing required arguments is rejected by validation before the tool runs.
    agent, _ = _scripted_agent(
        [
            [ToolCallPart("str_replace_editor", {"command": "create"})],
            [TextPart("giving up")],
            [_create("./repo.md")],
            [TextPart("done")],
        ]
    )
    with caplog.at_level(logging.WARNING):
        _run(agent, _deps(tmp_path), "repo", [])
    assert Path(tmp_path, "repo.md").exists()
    assert "rejected tool calls (1): str_replace_editor" in caplog.text


def test_doc_written_accepts_overview_for_whole_repo(tmp_path):
    assert not doc_written(str(tmp_path), "repo", [], {})
    Path(tmp_path, "overview.md").write_text("# x\n")
    assert doc_written(str(tmp_path), "repo", [], {})


def test_doc_written_finds_nested_and_sanitized_pages(tmp_path):
    tree = {"parent": {"children": {"Build Process": {"children": {}}}}}
    (tmp_path / "build_process.md").write_text("# Build\n")
    assert doc_written(str(tmp_path), "Build Process", ["parent", "Build Process"], tree)
    (tmp_path / "parent").mkdir()
    (tmp_path / "parent" / "child.md").write_text("# Child\n")
    tree["parent"]["children"]["child"] = {"children": {}}
    assert doc_written(str(tmp_path), "child", ["parent", "child"], tree)


def _backend(tmp_path, monkeypatch, kind, run):
    from codewiki.src.be.backend import UsageLedger
    from codewiki.src.config import Config

    config = Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        llm_api_key="test",
        llm_base_url="https://example.invalid",
        main_model="test",
        cluster_model="test",
    )
    if kind == "pydantic":
        from codewiki.src.be import pydantic_ai_backend as mod

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, prompt, deps, **kwargs):
                run(prompt, deps)
                return SimpleNamespace(
                    output="I'll write it", new_messages=lambda: [], all_messages=lambda: []
                )

        monkeypatch.setattr(mod, "Agent", FakeAgent)
        backend = mod.PydanticAIBackend.__new__(mod.PydanticAIBackend)
        backend._fallback_models = None
        backend._custom_instructions = ""
    else:
        pytest.importorskip("caw")
        from codewiki.src.be import caw_backend as mod

        class FakeAgent:
            def __init__(self, **kwargs):
                self.deps = kwargs["tool_servers"][0]._deps

            def completion(self, prompt):
                run(prompt, self.deps)
                return SimpleNamespace(
                    result="I'll write it", total_usage=None, num_turns=1, total_tool_calls=0
                )

        monkeypatch.setattr(mod, "CawAgent", FakeAgent)
        backend = mod.CawBackend.__new__(mod.CawBackend)
        backend._caw_provider = "codex"
        backend._model = "test"
        backend._repo_root = str(tmp_path)
    backend._config = config
    backend._usage_ledger = UsageLedger()
    (tmp_path / "module_tree.json").write_text("{}")
    return backend


@pytest.mark.parametrize("kind,expected_calls", [("pydantic", 3), ("caw", 1)])
def test_backend_failure_is_explicit_and_preserves_reserved_children(
    tmp_path, monkeypatch, kind, expected_calls
):
    calls = []

    def run(prompt, deps):
        calls.append(prompt)
        deps.module_tree["reserved"] = {"components": [], "children": {}}

    backend = _backend(tmp_path, monkeypatch, kind, run)
    cwd = Path.cwd()
    with pytest.raises(DocumentationWriteError, match="repo.md"):
        asyncio.run(backend.run_module_agent("repo", {}, [], [], str(tmp_path)))
    assert len(calls) == expected_calls
    assert "reserved" in json.loads((tmp_path / "module_tree.json").read_text())
    assert Path.cwd() == cwd


@pytest.mark.parametrize("kind", ["pydantic", "caw"])
@pytest.mark.parametrize("original,expected_calls", [("# Existing\n", 0), ("", 1)])
def test_whole_repo_backend_skips_valid_overview_and_recovers_empty_one(
    tmp_path, monkeypatch, kind, original, expected_calls
):
    calls = []

    def run(prompt, deps):
        calls.append(prompt)
        (tmp_path / "repo.md").write_text("# Repository\n\nComplete documentation.")

    backend = _backend(tmp_path, monkeypatch, kind, run)
    (tmp_path / "overview.md").write_text(original)
    asyncio.run(backend.run_module_agent("repo", {}, [], [], str(tmp_path)))
    assert len(calls) == expected_calls
    assert (tmp_path / "overview.md").read_text() == original
    if calls:
        assert "An existing root page is incomplete" in calls[0]


def test_subagent_failure_restores_dependencies(tmp_path, monkeypatch):
    from codewiki.src.be.agent_tools import generate_sub_module_documentations as mod
    from codewiki.src.be.dependency_analyzer.models.core import Node

    node = Node(
        id="a.py::A",
        name="A",
        component_type="class",
        file_path="a.py",
        relative_path="a.py",
        source_code="class A: pass",
    )
    deps = _deps(tmp_path)
    deps.components = {node.id: node}
    deps.scope_component_ids = [node.id]
    deps.config = SimpleNamespace(
        agent_retries=1, request_limit=10, max_token_per_leaf_module=100, layout="flat"
    )
    original_scope = deps.scope_component_ids

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

    async def fail(*args, **kwargs):
        raise DocumentationWriteError("missing child.md")

    monkeypatch.setattr(mod, "Agent", FakeAgent)
    monkeypatch.setattr(mod, "create_fallback_models", lambda config: None)
    monkeypatch.setattr(mod, "run_until_doc_written", fail)
    with pytest.raises(DocumentationWriteError):
        asyncio.run(
            mod.generate_sub_module_documentation(SimpleNamespace(deps=deps), {"child": [node.id]})
        )
    assert deps.current_module_name == "repo" and deps.current_depth == 1
    assert deps.path_to_current_module == []
    assert deps.scope_component_ids is original_scope
    assert "child" in deps.module_tree


@pytest.mark.parametrize("kind", ["pydantic", "caw"])
@pytest.mark.parametrize("fails", [False, True])
def test_whole_repo_generation_replaces_or_restores_incomplete_overview(
    tmp_path, monkeypatch, kind, fails
):
    from codewiki.src.be.documentation_generator import DocumentationGenerator

    original = "I'll start by reading the child files.\n\n/opt/child.md"
    page = tmp_path / "overview.md"

    def run(prompt, deps):
        if fails:
            page.write_text("partial edit")
            raise RuntimeError("agent failed")
        (tmp_path / "repo.md").write_text("# Repository\n\nComplete documentation.")

    backend = _backend(tmp_path, monkeypatch, kind, run)
    backend._config.repo_path = str(tmp_path / "repo")
    backend._config.docs_dir = str(tmp_path)
    page.write_text(original)
    gen = DocumentationGenerator.__new__(DocumentationGenerator)
    gen.backend = backend
    gen.config = backend._config
    if fails:
        with pytest.raises(RuntimeError, match="agent failed"):
            asyncio.run(gen.generate_module_documentation({}, []))
        assert page.read_text() == original
    else:
        asyncio.run(gen.generate_module_documentation({}, []))
        assert page.read_text() == "# Repository\n\nComplete documentation."
        assert not (tmp_path / "repo.md").exists()
