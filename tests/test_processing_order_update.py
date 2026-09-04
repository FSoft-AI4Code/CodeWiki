"""Tests for the module processing order used by generate_module_documentation.

Regression coverage for incremental updates (--update / --compare-to):
_invalidate_affected_modules deletes affected .md files based on
module_tree.json, so the regeneration loop must derive its processing order
from that same tree. first_module_tree.json only holds the initial clustering
result: it stays {} in whole-repository mode (issue #99) and never learns about
sub-modules that agents insert while documenting a complex module. Ordering
from it left invalidated modules unvisited and the run ended in
IncompleteDocumentationError.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from codewiki.src.be.documentation_generator import DocumentationGenerator

# --------------------------------------------------------------------------- #
# get_processing_order unit tests
# --------------------------------------------------------------------------- #


def test_flat_tree_preserves_insertion_order():
    tree = {
        "module_a": {"components": ["src/a.py::A"], "children": {}},
        "module_b": {"components": ["src/b.py::B"], "children": {}},
    }

    order = DocumentationGenerator.get_processing_order(tree)

    assert order == [(["module_a"], "module_a"), (["module_b"], "module_b")]


def test_nested_tree_is_leaf_first():
    tree = {
        "parent": {
            "components": [],
            "children": {
                "child": {"components": ["src/c.py::C"], "children": {}},
            },
        },
    }

    order = DocumentationGenerator.get_processing_order(tree)

    assert order == [(["parent", "child"], "child"), (["parent"], "parent")]


def test_empty_tree_returns_empty_order():
    assert DocumentationGenerator.get_processing_order({}) == []


# --------------------------------------------------------------------------- #
# generate_module_documentation integration tests (no LLM, fake backend)
# --------------------------------------------------------------------------- #


class FakeBackend:
    """Stand-in for LLMBackend that writes a doc file per module agent call."""

    def __init__(self, working_dir: Path):
        self.working_dir = working_dir
        self.module_agent_calls: list[str] = []
        self.complete_calls = 0

    async def run_module_agent(
        self, module_name, components, core_component_ids, module_path, working_dir
    ):
        module_tree = json.loads(Path(working_dir, "module_tree.json").read_text())
        # Mirror the real backends: a module whose doc already exists is skipped.
        if Path(working_dir, f"{module_name}.md").exists():
            return module_tree
        self.module_agent_calls.append(module_name)
        Path(working_dir, f"{module_name}.md").write_text(f"# {module_name}\n")
        return module_tree

    def complete(self, prompt, model=None):
        self.complete_calls += 1
        return "<OVERVIEW>overview</OVERVIEW>"


def _generator(docs_dir: Path) -> tuple[DocumentationGenerator, FakeBackend]:
    # Bypass __init__: it wires a real LLM backend and dependency analyzer.
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(docs_dir=str(docs_dir), repo_path=str(docs_dir))
    gen.backend = FakeBackend(docs_dir)
    return gen, gen.backend


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data))


def test_update_regenerates_agent_inserted_nested_module(tmp_path):
    """Clustered-repo case: first_module_tree.json knows only module_a, but the
    module_a agent later delegated sub1/sub2 into module_tree.json. A change
    under sub1 invalidates sub1.md, module_a.md and overview.md; all three must
    come back, and untouched sub2 must not be regenerated."""
    _write_json(
        tmp_path / "first_module_tree.json",
        {"module_a": {"components": ["src/a.py::A"], "children": {}}},
    )
    _write_json(
        tmp_path / "module_tree.json",
        {
            "module_a": {
                "components": ["src/a.py::A"],
                "children": {
                    "sub1": {"components": ["src/a.py::A.one"], "children": {}},
                    "sub2": {"components": ["src/a.py::A.two"], "children": {}},
                },
            }
        },
    )
    (tmp_path / "sub2.md").write_text("# sub2\n")
    gen, backend = _generator(tmp_path)

    asyncio.run(gen.generate_module_documentation(components={}, leaf_nodes=[]))

    assert (tmp_path / "sub1.md").exists()
    assert (tmp_path / "module_a.md").exists()
    assert (tmp_path / "overview.md").exists()
    assert backend.module_agent_calls == ["sub1"]
    # module_a (parent) + repository overview
    assert backend.complete_calls == 2


def test_update_regenerates_module_in_whole_repo_mode(tmp_path):
    """Issue #99: whole-repository mode leaves first_module_tree.json as {},
    while the whole-repo agent inserted module_a/module_b into
    module_tree.json. Invalidating module_a must regenerate it and the
    overview, and skip module_b whose doc is still present."""
    _write_json(tmp_path / "first_module_tree.json", {})
    _write_json(
        tmp_path / "module_tree.json",
        {
            "module_a": {"components": ["src/a.py::A"], "children": {}},
            "module_b": {"components": ["src/b.py::B"], "children": {}},
        },
    )
    (tmp_path / "module_b.md").write_text("# module_b\n")
    gen, backend = _generator(tmp_path)

    asyncio.run(gen.generate_module_documentation(components={}, leaf_nodes=[]))

    assert (tmp_path / "module_a.md").exists()
    assert (tmp_path / "overview.md").exists()
    assert backend.module_agent_calls == ["module_a"]
    assert backend.complete_calls == 1


def test_complete_docs_are_not_regenerated(tmp_path):
    """Plain re-run with everything already on disk must make no LLM calls,
    even though the full module_tree.json is now walked."""
    _write_json(tmp_path / "first_module_tree.json", {})
    _write_json(
        tmp_path / "module_tree.json",
        {
            "module_a": {
                "components": ["src/a.py::A"],
                "children": {"sub1": {"components": ["src/a.py::A.one"], "children": {}}},
            }
        },
    )
    for name in ("sub1", "module_a", "overview"):
        (tmp_path / f"{name}.md").write_text(f"# {name}\n")
    gen, backend = _generator(tmp_path)

    asyncio.run(gen.generate_module_documentation(components={}, leaf_nodes=[]))

    assert backend.module_agent_calls == []
    assert backend.complete_calls == 0
