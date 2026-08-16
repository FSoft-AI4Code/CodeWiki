"""Tests for build_overview_structure's prompt-size discipline.

Overview prompts used to inline the whole module tree (every module's
``components`` list) plus the full markdown of every child doc. On large
repos that blew past provider input caps — codex rejects any turn over
1,048,576 characters — and every parent/repo overview failed. The structure
must now strip ``components`` and reference children docs by absolute
``docs_path`` instead of inlining them.
"""

from __future__ import annotations

import json
import os

from codewiki.src.be.documentation_generator import DocumentationGenerator

CODEX_INPUT_CAP = 1_048_576


def _generator() -> DocumentationGenerator:
    # build_overview_structure touches neither config nor backend; skip the
    # heavyweight __init__ (graph builder, backend resolution).
    return DocumentationGenerator.__new__(DocumentationGenerator)


def _make_tree(n_modules: int, n_components: int) -> dict:
    return {
        f"module_{i}": {
            "components": [f"src/file_{i}_{j}.c::func_{j}" for j in range(n_components)],
            "children": {
                f"module_{i}_child": {
                    "components": [f"src/file_{i}_{j}.c::helper_{j}" for j in range(n_components)],
                    "children": {},
                }
            },
        }
        for i in range(n_modules)
    }


def test_components_are_stripped_at_every_depth(tmp_path):
    tree = _make_tree(n_modules=3, n_components=5)
    result = _generator().build_overview_structure(tree, [], str(tmp_path))
    assert "components" not in json.dumps(result)
    # The original tree is untouched (deepcopy semantics).
    assert tree["module_0"]["components"]


def test_children_docs_referenced_by_absolute_path_not_inlined(tmp_path):
    tree = _make_tree(n_modules=2, n_components=1)
    doc = tmp_path / "module_0.md"
    doc.write_text("# module_0\n\nA very long body that must not appear in the prompt.")

    result = _generator().build_overview_structure(tree, [], str(tmp_path))

    assert result["module_0"]["docs_path"] == str(doc)
    assert os.path.isabs(result["module_0"]["docs_path"])
    assert result["module_1"]["docs_path"] is None  # missing doc -> null, no crash
    assert "must not appear" not in json.dumps(result)


def test_target_module_is_marked(tmp_path):
    tree = _make_tree(n_modules=2, n_components=1)
    result = _generator().build_overview_structure(tree, ["module_1"], str(tmp_path))
    assert result["module_1"]["is_target_for_overview_generation"] is True
    # Children of the target get docs_path entries.
    assert "docs_path" in result["module_1"]["children"]["module_1_child"]


def test_wazuh_scale_tree_stays_under_codex_input_cap(tmp_path):
    # Modeled on the real failure: ~30 top-level modules, hundreds of
    # components each, which serialized to ~2.1M chars before the fix.
    tree = _make_tree(n_modules=30, n_components=600)
    assert len(json.dumps(tree, indent=2)) > CODEX_INPUT_CAP  # the old shape overflowed

    result = _generator().build_overview_structure(tree, [], str(tmp_path))
    assert len(json.dumps(result, indent=2)) < CODEX_INPUT_CAP / 10
