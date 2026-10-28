"""Mermaid integration across writing backends, prompts and overview generation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codewiki.mcp.session import SessionStore
from codewiki.mcp.tools.doc_writer import handle_edit_doc_file, handle_write_doc_file
from codewiki.mcp.tools.prompt_server import _resolve_prompt
from codewiki.src.be import utils
from codewiki.src.be.agent_tools.str_replace_editor import EditTool, str_replace_editor
from codewiki.src.be.backend import UsageLedger
from codewiki.src.be.caw_toolkit import CawToolKit
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.mermaid_support import DiagramResult, MERMAID_GUIDELINES
from codewiki.src.be.updater.prompts import (
    format_stale_fix_system_prompt,
    format_update_system_prompt,
)

INVALID = "```mermaid\nflowchart TD\nC{msg.error()?}\n```\n"
FIXED = INVALID.replace("C{msg.error()?}", 'C{"msg.error()?"}')
BROKEN = "```mermaid\nflowchart TD\nbroken\n```\n"


@pytest.fixture
def parser(monkeypatch):
    calls = []

    async def parse(source):
        calls.append(source)
        if "C{msg.error()?}" in source:
            return DiagramResult("invalid", "Parse error on line 2: got 'PS'")
        if "broken" in source:
            return DiagramResult("invalid", "Parse error on line 2: got 'TEXT'")
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    return calls


@pytest.fixture(params=["pydantic", "caw"])
def editor(request, tmp_path):
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path / "repo"),
        allowed_write_paths=None,
    )
    toolkit = CawToolKit(deps, backend=None, allow_subagent=False)

    async def call(command, **kwargs):
        if request.param == "caw":
            return await toolkit.str_replace_editor("docs", command, path="x.md", **kwargs)
        return await str_replace_editor(
            SimpleNamespace(deps=deps), "docs", command, path="x.md", **kwargs
        )

    return call


def test_editor_repairs_and_returns_saved_snippet_and_undoes(tmp_path, editor, parser):
    path = tmp_path / "x.md"
    # Undo must restore even an invalid pre-existing document exactly.
    original = "# Before\n" + INVALID
    path.write_text(original)

    async def check():
        result = await editor("str_replace", old_str="# Before", new_str="# After")
        assert path.read_text() == "# After\n" + FIXED
        assert 'C{"msg.error()?"}' in result.split("Mermaid validation")[0]
        assert "repaired and validated" in result
        result = await editor("undo_edit")
        assert "undone successfully" in result
        assert path.read_text() == original
        assert "syntax errors" in result and "repaired and validated" not in result

    asyncio.run(check())
    assert len(parser) == 3


@pytest.mark.parametrize("command", ["create", "insert"])
def test_editor_create_and_insert(tmp_path, editor, parser, command):
    path = tmp_path / "x.md"
    kwargs = {"file_text": INVALID}
    if command == "insert":
        path.write_text("# Title\n")
        kwargs = {"insert_line": 1, "new_str": INVALID}
    result = asyncio.run(editor(command, **kwargs))
    assert FIXED.strip() in path.read_text()
    assert "repaired and validated" in result


def test_editor_failed_edits_and_views_do_not_repair(tmp_path, editor, parser):
    path = tmp_path / "x.md"
    path.write_text(INVALID)

    async def check():
        await editor("view")
        await editor("str_replace", old_str="missing", new_str="replacement")
        await editor("create", file_text="replace existing")
        await editor("insert", insert_line=-1, new_str="bad position")
        assert path.read_text() == INVALID
        assert not parser

    asyncio.run(check())


def test_failed_disk_write_does_not_create_history(tmp_path, monkeypatch):
    path = tmp_path / "x.md"
    path.write_text("old")
    tool = EditTool({}, str(tmp_path))
    original_open = Path.open

    def fail(path, mode="r", *args, **kwargs):
        if mode == "w":
            raise OSError("disk unavailable")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    tool(command="str_replace", path=str(path), old_str="old", new_str="new")
    assert not tool.did_write and not tool._file_history
    assert not any("has been edited" in line for line in tool.logs)
    monkeypatch.undo()
    assert path.read_text() == "old"


def test_mcp_write_edit_insert_undo_and_failed_edit(tmp_path, parser):
    store = SessionStore()
    session = store.create(str(tmp_path), str(tmp_path), {}, [])
    args = {"session_id": session.session_id, "filename": "nested/x.md"}
    path = tmp_path / "nested/x.md"

    async def check():
        created = json.loads(await handle_write_doc_file({**args, "content": INVALID}, store))
        assert path.read_text() == FIXED
        assert "repaired and validated" in created["mermaid_validation"]
        # Replacing the repaired label proves agents can use persisted content.
        edited = json.loads(
            await handle_edit_doc_file(
                {**args, "command": "str_replace", "old_str": FIXED, "new_str": INVALID}, store
            )
        )
        assert 'C{"msg.error()?"}' in edited["snippet"] and path.read_text() == FIXED
        await handle_edit_doc_file({**args, "command": "undo"}, store)
        assert path.read_text() == FIXED
        await handle_edit_doc_file(
            {**args, "command": "insert", "insert_line": 0, "new_str": "# Intro"}, store
        )
        assert path.read_text().startswith("# Intro")
        await handle_edit_doc_file({**args, "command": "undo"}, store)
        assert path.read_text() == FIXED
        before_calls = len(parser)
        failed = json.loads(
            await handle_edit_doc_file(
                {**args, "command": "str_replace", "old_str": "missing"}, store
            )
        )
        assert "error" in failed and len(parser) == before_calls
        # Restoring a historical invalid document must not silently re-repair it.
        session.registry["file_history"] = {str(path): [INVALID]}
        result = json.loads(await handle_edit_doc_file({**args, "command": "undo"}, store))
        assert path.read_text() == INVALID and "syntax errors" in result["mermaid_validation"]

    asyncio.run(check())


class Backend:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.usage_ledger = UsageLedger()

    def complete(self, prompt, system_prompt=None):
        self.calls.append((prompt, system_prompt))
        self.usage_ledger.add("complete", "", {"input_tokens": 10})
        return next(self.replies)


def generator(tmp_path, replies):
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        repo_path=str(tmp_path), layout="flat", get_prompt_addition=lambda: "Write in Portuguese."
    )
    gen.backend = Backend(replies)
    tree = {"parent": {"components": [], "children": {"leaf": {"components": [], "children": {}}}}}
    (tmp_path / "module_tree.json").write_text(json.dumps(tree))
    return gen


@pytest.mark.parametrize("module_path,filename", [([], "overview.md"), (["parent"], "parent.md")])
def test_overview_repairs_before_saving(tmp_path, parser, module_path, filename):
    gen = generator(tmp_path, ["<OVERVIEW>" + INVALID + "</OVERVIEW>"])
    asyncio.run(gen.generate_parent_module_docs(module_path, str(tmp_path)))
    assert (tmp_path / filename).read_text() == FIXED.strip()
    assert len(gen.backend.calls) == 1


def test_overview_retries_once_and_tracks_usage(tmp_path, parser):
    gen = generator(tmp_path, [BROKEN, FIXED])
    asyncio.run(gen.generate_parent_module_docs([], str(tmp_path)))
    assert (tmp_path / "overview.md").read_text() == FIXED.strip()
    assert len(gen.backend.calls) == 2
    correction, system = gen.backend.calls[1]
    assert "<PREVIOUS_DRAFT>" in correction and "Parse error" in correction
    assert MERMAID_GUIDELINES in correction and "Portuguese" in system
    usage = gen.backend.usage_ledger.summary()
    assert usage["n_calls"] == 2 and usage["by_phase"]["overview"]["calls"] == 2


def test_invalid_overview_after_retry_is_not_saved(tmp_path, parser):
    gen = generator(tmp_path, [BROKEN, BROKEN])
    with pytest.raises(RuntimeError, match="after one correction attempt"):
        asyncio.run(gen.generate_parent_module_docs([], str(tmp_path)))
    assert not (tmp_path / "overview.md").exists()
    assert len(gen.backend.calls) == 2


def test_overview_unavailable_warns_without_retry(tmp_path, monkeypatch, caplog):
    async def unavailable(_):
        return DiagramResult("unavailable", "HTTP 503")

    monkeypatch.setattr(utils, "_validate_diagram", unavailable)
    gen = generator(tmp_path, [INVALID])
    asyncio.run(gen.generate_parent_module_docs([], str(tmp_path)))
    assert (tmp_path / "overview.md").read_text() == INVALID.strip()
    assert len(gen.backend.calls) == 1
    assert "validation unavailable" in caplog.text and "HTTP 503" in caplog.text


@pytest.mark.parametrize(
    "prompt_type", ["system_leaf", "system_complex", "overview_module", "overview_repo"]
)
def test_generation_prompts_share_formatted_guidance(prompt_type):
    prompt = _resolve_prompt(
        prompt_type,
        {
            "module_name": "example",
            "repo_name": "example",
            "custom_instructions": "Write in Portuguese.",
        },
    )
    assert MERMAID_GUIDELINES in prompt
    assert "{{msg" not in prompt and 'C{"msg.error()?"}' in prompt
    if prompt_type.startswith("system_"):
        assert "Portuguese" in prompt


def test_updater_prompts_and_skill_share_guidance():
    assert MERMAID_GUIDELINES in format_update_system_prompt("leaf", "Portuguese")
    assert MERMAID_GUIDELINES in format_stale_fix_system_prompt("Portuguese")
    skill = (Path(__file__).parents[1] / "skills/codewiki-wiki-generator/SKILL.md").read_text()
    for example in [
        'C{"msg.error()?"}',
        'D["config[key]"]',
        "A->>B: first#59; second",
        'A -- "start -- stop" --> B',
    ]:
        assert example in skill and example in MERMAID_GUIDELINES


def test_editor_undo_preserves_tabs_and_crlf(tmp_path, editor, parser):
    path = tmp_path / "x.md"
    original = ("# Before\n\ttext\n" + INVALID).replace("\n", "\r\n").encode()
    path.write_bytes(original)

    async def check():
        await editor("str_replace", old_str="# Before", new_str="# After")
        await editor("undo_edit")

    asyncio.run(check())
    assert path.read_bytes() == original
