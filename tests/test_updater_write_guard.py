"""The str_replace_editor write-set guard used by the incremental updater."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from codewiki.src.be.agent_tools.str_replace_editor import check_write_allowed, str_replace_editor


def _ctx(docs_dir, allowed):
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(docs_dir),
        absolute_repo_path=str(docs_dir / "repo"),
        allowed_write_paths=allowed,
    )
    return SimpleNamespace(deps=deps)


def test_check_write_allowed_none_is_unrestricted(tmp_path):
    assert check_write_allowed(str(tmp_path / "x.md"), None) is None


def test_check_write_allowed_resolves_paths(tmp_path):
    page = tmp_path / "a.md"
    page.write_text("x")
    allowed = {str(tmp_path / "." / "a.md")}
    assert check_write_allowed(str(page), allowed) is None
    denied = check_write_allowed(str(tmp_path / "b.md"), allowed)
    assert denied and "not in this agent's write set" in denied and "a.md" in denied


def test_editor_refuses_pages_outside_write_set(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "a.md").write_text("hello a\n")
    (tmp_path / "b.md").write_text("hello b\n")
    allowed = {str(tmp_path / "a.md")}

    out = asyncio.run(
        str_replace_editor(
            _ctx(tmp_path, allowed),
            "docs",
            "str_replace",
            path="b.md",
            old_str="hello b",
            new_str="bye b",
        )
    )
    assert "not in this agent's write set" in out
    assert (tmp_path / "b.md").read_text() == "hello b\n"

    out = asyncio.run(
        str_replace_editor(
            _ctx(tmp_path, allowed),
            "docs",
            "str_replace",
            path="a.md",
            old_str="hello a",
            new_str="bye a",
        )
    )
    assert "not in this agent's write set" not in out
    assert (tmp_path / "a.md").read_text() == "bye a\n"

    # view stays allowed everywhere
    out = asyncio.run(str_replace_editor(_ctx(tmp_path, allowed), "docs", "view", path="b.md"))
    assert "hello b" in out

    # create of a page outside the set is refused too
    out = asyncio.run(
        str_replace_editor(_ctx(tmp_path, allowed), "docs", "create", path="c.md", file_text="new")
    )
    assert "not in this agent's write set" in out
    assert not (tmp_path / "c.md").exists()


def test_editor_rejects_absolute_and_escaping_paths(tmp_path):
    (tmp_path / "repo").mkdir()
    out = asyncio.run(
        str_replace_editor(_ctx(tmp_path, None), "docs", "view", path=str(tmp_path / "a.md"))
    )
    assert "must be relative" in out
    out = asyncio.run(str_replace_editor(_ctx(tmp_path, None), "docs", "view", path="../x.md"))
    assert "escapes" in out


def test_usage_to_dict_handles_property_and_method():
    from types import SimpleNamespace

    from codewiki.src.be.backend import usage_to_dict
    from codewiki.src.be.pydantic_ai_backend import _run_usage

    usage = SimpleNamespace(input_tokens=10, output_tokens=3, requests=1, cost=None, details={})
    assert usage_to_dict(usage) == {"input_tokens": 10, "output_tokens": 3, "requests": 1}
    assert _run_usage(SimpleNamespace(usage=usage)) == {
        "input_tokens": 10,
        "output_tokens": 3,
        "requests": 1,
    }
    assert _run_usage(SimpleNamespace(usage=lambda: usage)) == {
        "input_tokens": 10,
        "output_tokens": 3,
        "requests": 1,
    }
    assert _run_usage(SimpleNamespace()) is None
