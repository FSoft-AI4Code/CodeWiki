"""Tests for DocumentationGenerator.resolve_processing_order.

Covers the whole-repository-mode incremental-update bug: first_module_tree.json
is only ever written by the LLM clustering path and stays {} for repos small
enough to skip clustering, even after a prior whole-repo agent run inserts
real sub-modules into module_tree.json. Without falling back to module_tree,
--update/--compare-to invalidates (deletes) an affected sub-module's .md but
the processing loop never re-visits it, so the run always ends in
IncompleteGenerationError.
"""

from __future__ import annotations

from codewiki.src.be.documentation_generator import DocumentationGenerator


def _generator() -> DocumentationGenerator:
    # get_processing_order/resolve_processing_order don't touch any instance
    # state (config/backend/graph_builder), so constructing without __init__
    # keeps this a pure unit test with no Config/LLM backend wiring needed.
    return object.__new__(DocumentationGenerator)


def test_falls_back_to_module_tree_when_first_module_tree_empty():
    """Whole-repository-mode case: first_module_tree.json is {}, but
    module_tree.json carries sub-modules a prior whole-repo agent run
    inserted. The invalidated leaf must still show up in processing order."""
    gen = _generator()
    module_tree = {
        "module_a": {"components": ["src/a.py::A"], "children": {}},
        "module_b": {"components": ["src/b.py::B"], "children": {}},
    }

    order = gen.resolve_processing_order({}, module_tree)

    assert [name for _, name in order] == ["module_a", "module_b"]


def test_uses_first_module_tree_when_non_empty():
    """Normal clustered-repo case is unaffected: first_module_tree.json is
    non-empty, so its order is used as before and module_tree is ignored."""
    gen = _generator()
    first_module_tree = {"module_a": {"components": ["src/a.py::A"], "children": {}}}
    # module_tree has since gained an extra nested module the agent
    # inserted while processing — resolve_processing_order must not pick
    # that up, since first_module_tree already produced a non-empty order.
    module_tree = {
        "module_a": {
            "components": ["src/a.py::A"],
            "children": {"sub": {"components": ["src/a.py::A.helper"], "children": {}}},
        },
    }

    order = gen.resolve_processing_order(first_module_tree, module_tree)

    assert [name for _, name in order] == ["module_a"]


def test_both_trees_empty_returns_empty_order():
    """A genuinely fresh whole-repo-mode run (nothing inserted into
    module_tree.json yet) must not crash — it just returns no processing
    order, and generate_module_documentation's whole-repo branch handles it."""
    gen = _generator()

    order = gen.resolve_processing_order({}, {})

    assert order == []


def test_nested_children_preserved_in_fallback_order():
    """The fallback still walks nested children leaf-first, matching
    get_processing_order's normal topological (leaf-first) contract."""
    gen = _generator()
    module_tree = {
        "parent": {
            "components": [],
            "children": {
                "child": {"components": ["src/c.py::C"], "children": {}},
            },
        },
    }

    order = gen.resolve_processing_order({}, module_tree)

    assert [name for _, name in order] == ["child", "parent"]
