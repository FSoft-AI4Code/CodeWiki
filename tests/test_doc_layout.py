"""Docs layout (issue #125): pages mirror the module tree by default, or sit flat."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from codewiki.src.be import doc_layout as L
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.module_naming import find_missing_module_docs, sub_module_report
from codewiki.src.be.prompt_template import (
    DART_FLUTTER_NOTE,
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.updater import pages as P

TREE = {
    "auth": {
        "components": ["a.py::A"],
        "children": {
            "login": {"components": ["a.py::login"], "children": {}},
            "session": {
                "components": ["a.py::S"],
                "children": {"store": {"components": ["a.py::store"], "children": {}}},
            },
        },
    },
    "billing": {"components": ["b.py::B"], "children": {}},
}


def _write(docs: Path, rel: str, text: str = "# page\n") -> Path:
    path = docs / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _setup(docs: Path, layout: str | None = "hierarchical") -> None:
    docs.mkdir(exist_ok=True)
    (docs / "module_tree.json").write_text(json.dumps(TREE))
    if layout is not None:
        (docs / "metadata.json").write_text(json.dumps({"generation_info": {"layout": layout}}))


# --------------------------------------------------------------------- paths


def test_module_doc_relpath_per_layout():
    assert L.module_doc_relpath([], "hierarchical") == "overview.md"
    assert L.module_doc_relpath(["auth"], "hierarchical") == "auth.md"
    assert L.module_doc_relpath(["auth", "session", "store"], "hierarchical") == (
        "auth/session/store.md"
    )
    assert L.module_doc_relpath(["auth", "session", "store"], "flat") == "store.md"


def test_doc_relpaths_cover_tree_and_overview():
    paths = L.doc_relpaths(TREE, "hierarchical")
    assert paths == {
        "overview": "overview.md",
        "auth": "auth.md",
        "login": "auth/login.md",
        "session": "auth/session.md",
        "store": "auth/session/store.md",
        "billing": "billing.md",
    }
    assert L.doc_relpaths(TREE, "flat")["store"] == "store.md"


def test_read_layout_defaults_to_flat_for_old_docs(tmp_path):
    assert L.read_layout(str(tmp_path)) == "flat"
    (tmp_path / "metadata.json").write_text(json.dumps({"generation_info": {"language": None}}))
    assert L.read_layout(str(tmp_path)) == "flat"
    assert L.docs_layout(str(tmp_path)) == "flat"
    (tmp_path / "metadata.json").write_text(
        json.dumps({"generation_info": {"layout": "hierarchical"}})
    )
    assert L.read_layout(str(tmp_path)) == "hierarchical"


def test_docs_layout_defaults_to_hierarchical_for_new_docs(tmp_path):
    assert L.docs_layout(str(tmp_path)) == "hierarchical"


def test_find_doc_and_listing_work_in_both_layouts(tmp_path):
    _setup(tmp_path)
    _write(tmp_path, "overview.md")
    _write(tmp_path, "auth/session/store.md")
    _write(tmp_path, "billing.md")
    _write(tmp_path, "temp/ignored.md")
    _write(tmp_path, "guides/user_notes.md")  # not a module page

    assert L.find_doc(str(tmp_path), "store") == str(tmp_path / "auth/session/store.md")
    assert L.find_doc(str(tmp_path), "login") is None
    # a page saved flat is still found
    _write(tmp_path, "login.md")
    assert L.find_doc(str(tmp_path), "login") == str(tmp_path / "login.md")

    assert L.list_doc_files(str(tmp_path)) == {
        "overview": "overview.md",
        "store": "auth/session/store.md",
        "billing": "billing.md",
        "login": "login.md",
    }


def test_find_doc_search_finds_page_dropped_from_tree(tmp_path):
    _setup(tmp_path)
    _write(tmp_path, "auth/old_module.md")
    assert L.find_doc(str(tmp_path), "old_module") is None
    assert L.find_doc(str(tmp_path), "old_module", search=True) == str(
        tmp_path / "auth/old_module.md"
    )


# --------------------------------------------------------------------- links


def test_rewrite_page_links_fixes_relative_paths():
    paths = L.doc_relpaths(TREE, "hierarchical")
    text = (
        "See [billing](billing.md), [store](store.md#api), [login](./login.md), "
        '[wrong](../../billing.md "Billing"), [web](https://x.io/a.md), '
        "[unknown](nope.md) and [overview](overview.md).\n"
        "```\n[in code](billing.md)\n```\n"
    )
    out = L.rewrite_page_links(text, "auth/session.md", paths)
    assert "[billing](../billing.md)" in out
    assert "[store](session/store.md#api)" in out
    assert "[login](login.md)" in out
    assert '[wrong](../billing.md "Billing")' in out
    assert "[web](https://x.io/a.md)" in out
    assert "[unknown](nope.md)" in out
    assert "[overview](../overview.md)" in out
    assert "[in code](billing.md)" in out  # fenced code is left alone


def test_rewrite_page_links_is_identity_for_flat_layout():
    paths = L.doc_relpaths(TREE, "flat")
    text = "[a](auth.md) [s](store.md#x) [o](overview.md)\n"
    assert L.rewrite_page_links(text, "login.md", paths) == text


def test_organize_docs_moves_misplaced_pages_and_fixes_links(tmp_path):
    _setup(tmp_path)
    _write(tmp_path, "overview.md", "[auth](auth.md) [store](store.md)\n")
    _write(tmp_path, "auth.md", "[login](login.md)\n")
    _write(tmp_path, "login.md", "[billing](billing.md) [store](store.md)\n")  # misplaced
    _write(tmp_path, "auth/session.md")
    _write(tmp_path, "store.md")  # misplaced
    _write(tmp_path, "billing.md")

    L.organize_docs(str(tmp_path), "hierarchical")

    assert not (tmp_path / "login.md").exists()
    assert not (tmp_path / "store.md").exists()
    assert (tmp_path / "auth/login.md").read_text() == (
        "[billing](../billing.md) [store](session/store.md)\n"
    )
    assert (tmp_path / "auth/session/store.md").exists()
    assert (tmp_path / "auth.md").read_text() == "[login](auth/login.md)\n"
    assert (tmp_path / "overview.md").read_text() == (
        "[auth](auth.md) [store](auth/session/store.md)\n"
    )


def test_organize_docs_flattens_nested_pages(tmp_path):
    _setup(tmp_path, layout="flat")
    _write(tmp_path, "auth/session/store.md", "[billing](../../billing.md)\n")
    _write(tmp_path, "billing.md")

    L.organize_docs(str(tmp_path), "flat")

    assert (tmp_path / "store.md").read_text() == "[billing](billing.md)\n"
    assert not (tmp_path / "auth").exists()  # emptied folders are removed


# ------------------------------------------------------------------- prompts


def test_prompts_name_the_nested_page_path():
    path = ["auth", "session"]
    system = format_system_prompt("session", None, path, "hierarchical")
    assert "`auth/session.md`" in system
    assert "under `auth/session/`" in system
    assert "auth/session.md documentation file" in format_leaf_system_prompt(
        "session", None, path, "hierarchical"
    )
    user = format_user_prompt("session", [], {}, TREE, path, "hierarchical")
    assert "store [page: auth/session/store.md]" in user
    assert "Your page is `auth/session.md`" in user


def test_flat_prompts_keep_flat_wording():
    system = format_system_prompt("session", None, ["auth", "session"], "flat")
    assert "`session.md`" in system
    assert "all docs share one flat directory" in system
    user = format_user_prompt("session", [], {}, TREE, ["auth", "session"], "flat")
    assert "[page:" not in user
    assert "saved in the same folder" in user


def test_sub_module_report_gives_links_from_parent_page(tmp_path):
    _setup(tmp_path)
    _write(tmp_path, "auth/session/store.md")
    report = sub_module_report(
        {"store": "store", "cache": "cache"},
        {},
        str(tmp_path),
        TREE,
        "session",
        ["auth", "session"],
        "hierarchical",
    )
    assert "link them from `auth/session.md`" in report
    assert "Saved documentations (link them from `auth/session.md` as): session/store.md." in (
        report
    )
    assert "MISSING (generation did not produce these files): session/cache.md" in report


# ---------------------------------------------------------------- generation


class _FakeBackend:
    """Writes each leaf page where the hierarchical layout expects it."""

    def __init__(self):
        self.calls: list[str] = []

    async def run_module_agent(
        self, module_name, components, core_component_ids, module_path, working_dir
    ):
        tree = json.loads(Path(working_dir, "module_tree.json").read_text())
        if L.find_doc(working_dir, module_name, tree):
            return tree
        self.calls.append(module_name)
        rel = L.module_doc_relpath(module_path, "hierarchical")
        _write(Path(working_dir), rel, f"# {module_name}\n\n[billing](billing.md)\n")
        return tree

    def complete(self, prompt, model=None, system_prompt=None):
        return "<OVERVIEW>[store](store.md)</OVERVIEW>"


def test_generation_writes_nested_pages(tmp_path):
    _setup(tmp_path, layout=None)
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        docs_dir=str(tmp_path),
        repo_path=str(tmp_path),
        get_prompt_addition=lambda: "",
        layout="hierarchical",
    )
    gen.backend = _FakeBackend()

    asyncio.run(gen.generate_module_documentation(components={}, leaf_nodes=[]))

    for rel in ("overview.md", "auth.md", "auth/login.md", "auth/session.md", "billing.md"):
        assert (tmp_path / rel).exists(), rel
    assert (tmp_path / "auth/session/store.md").read_text() == (
        "# store\n\n[billing](../../billing.md)\n"
    )
    assert (tmp_path / "auth.md").read_text() == "[store](auth/session/store.md)"
    assert find_missing_module_docs(TREE, str(tmp_path)) == []

    # Resume: everything exists, nothing is regenerated
    calls = list(gen.backend.calls)
    asyncio.run(gen.generate_module_documentation(components={}, leaf_nodes=[]))
    assert gen.backend.calls == calls


# ------------------------------------------------------------------- updater


def test_updater_pages_follow_recorded_layout(tmp_path):
    _setup(tmp_path, layout="hierarchical")
    _write(tmp_path, "auth/login.md", "old")
    assert P.page_rel(str(tmp_path), "login") == "auth/login.md"
    assert P.read_page(str(tmp_path), "login") == "old"
    # a new page goes where the layout wants it
    assert P.page_rel(str(tmp_path), "store") == "auth/session/store.md"
    assert P.list_pages(str(tmp_path)) == ["login"]
    assert P.remove_page(str(tmp_path), "login")
    assert not (tmp_path / "auth").exists()

    flat = tmp_path / "flat"
    _setup(flat, layout=None)  # old docs: no layout recorded -> flat
    assert P.page_rel(str(flat), "store") == "store.md"


# -------------------------------------------------------------- editor/viewer


def _editor_ctx(docs: Path):
    deps = SimpleNamespace(
        registry={},
        absolute_docs_path=str(docs),
        absolute_repo_path=str(docs / "repo"),
        allowed_write_paths=None,
    )
    return SimpleNamespace(deps=deps)


def test_editor_creates_nested_page_folders(tmp_path):
    from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor

    (tmp_path / "repo").mkdir()
    out = asyncio.run(
        str_replace_editor(
            _editor_ctx(tmp_path), "docs", "create", path="auth/session/store.md", file_text="# s\n"
        )
    )
    assert "File created successfully" in out
    assert (tmp_path / "auth/session/store.md").read_text() == "# s\n"

    out = asyncio.run(
        str_replace_editor(
            _editor_ctx(tmp_path), "docs", "create", path="../escape/x.md", file_text="x"
        )
    )
    assert "escapes" in out
    assert not (tmp_path.parent / "escape").exists()


def test_viewer_embeds_page_paths(tmp_path):
    from codewiki.cli.html_generator import HTMLGenerator

    _setup(tmp_path)
    _write(tmp_path, "overview.md")
    _write(tmp_path, "auth/session/store.md")
    out = tmp_path / "index.html"
    HTMLGenerator().generate(output_path=out, title="repo", docs_dir=tmp_path)
    html = out.read_text(encoding="utf-8")
    assert '"store": "auth/session/store.md"' in html
    assert "{{DOC_PATHS_JSON}}" not in html


def test_metadata_records_layout(tmp_path):
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(main_model="m", repo_path=".", max_depth=2, layout="flat")
    gen.commit_id = None
    _setup(tmp_path, layout=None)
    _write(tmp_path, "auth/login.md")
    gen.create_documentation_metadata(str(tmp_path), {}, 0)
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["generation_info"]["layout"] == "flat"
    assert "auth/login.md" in metadata["files_generated"]


def test_module_names_drop_ampersand():
    from codewiki.src.be.module_naming import dedupe_module_tree_names, sanitize_module_name

    assert sanitize_module_name("Data_Model_&_Persistence") == "Data_Model___Persistence"
    tree = dedupe_module_tree_names({"A & B": {"components": [], "children": {}}})
    assert list(tree) == ["A___B"]


def _component(tmp_path, relpath: str, cid: str) -> Node:
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// code\n", encoding="utf-8")
    return Node(
        id=cid,
        name=cid.split("::")[-1],
        component_type="class",
        file_path=str(path),
        relative_path=relpath,
    )


def test_dart_modules_get_flutter_note(tmp_path):
    components = {
        "lib/home.dart::HomePage": _component(tmp_path, "lib/home.dart", "lib/home.dart::HomePage")
    }
    prompt = format_user_prompt("auth", list(components), components, TREE, ["auth"], "flat")
    assert DART_FLUTTER_NOTE in prompt
    assert "```dart" in prompt


def test_non_dart_modules_have_no_flutter_note(tmp_path):
    components = {"a.py::A": _component(tmp_path, "a.py", "a.py::A")}
    prompt = format_user_prompt("auth", list(components), components, TREE, ["auth"], "flat")
    assert DART_FLUTTER_NOTE not in prompt
