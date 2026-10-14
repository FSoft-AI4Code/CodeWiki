"""An agent that ends without writing its page is reminded to write it (issue #134)."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.doc_guard import (
    DOC_WRITE_REMINDERS,
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
        calls.append(messages)
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
    with caplog.at_level(logging.WARNING):
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
