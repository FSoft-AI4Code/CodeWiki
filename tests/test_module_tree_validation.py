"""Tests for save_module_tree validation of component ids.

Verifies that a module tree referencing unknown/stale component ids is
surfaced in the response (unmatched_ids) and that indexed components left
out of the tree are reported as a coverage gap (leftover_component_ids),
without breaking the save itself.
"""

from __future__ import annotations

import json

from codewiki.mcp.session import SessionState, SessionStore
from codewiki.mcp.tools.module_tree import handle_save_module_tree
from codewiki.mcp.workspace import SessionWorkspace
from codewiki.src.be.dependency_analyzer.models.core import Node


def _make_node(component_id: str) -> Node:
    rel_path, _, name = component_id.partition("::")
    return Node(
        id=component_id,
        name=name or component_id,
        component_type="class",
        file_path=rel_path,
        relative_path=rel_path,
    )


def _make_session(
    store: SessionStore,
    tmp_path,
    component_ids: list[str],
) -> SessionState:
    components = {cid: _make_node(cid) for cid in component_ids}
    session = store.create(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        components=components,
        leaf_nodes=list(component_ids),
    )
    session.workspace = SessionWorkspace(tmp_path, session.session_id)
    return session


def _save(tree: dict, session: SessionState, store: SessionStore) -> dict:
    result = handle_save_module_tree(
        {"session_id": session.session_id, "module_tree": tree},
        store,
    )
    return json.loads(result)


def _read_validation_file(session: SessionState) -> dict:
    assert session.workspace is not None
    validation_path = session.workspace.root / "module_tree_validation.json"
    assert validation_path.exists(), "module_tree_validation.json not written"
    return json.loads(validation_path.read_text(encoding="utf-8"))


def test_valid_tree_no_gaps(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    session = _make_session(store, tmp_path, ids)
    tree = {
        "core": {"components": ["src/a.py::A"]},
        "utils": {"components": ["src/b.py::B", "src/c.py::C"]},
    }

    result = _save(tree, session, store)

    assert result["status"] == "saved"
    assert result["module_count"] == 2
    assert result["validation"]["unmatched_ids"] == []
    assert result["validation"]["unmatched_count"] == 0
    assert result["validation"]["leftover_component_count"] == 0
    assert "warning" not in result

    validation = _read_validation_file(session)
    assert validation["unmatched_ids"] == []
    assert validation["leftover_component_ids"] == []


def test_orphaned_id_reported_but_saved(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B"]
    session = _make_session(store, tmp_path, ids)
    tree = {
        "core": {"components": ["src/a.py::A", "src/a.py::Typo"]},
        "utils": {"components": ["src/b.py::B"]},
    }

    result = _save(tree, session, store)

    assert result["status"] == "saved"
    assert result["validation"]["unmatched_ids"] == ["src/a.py::Typo"]
    assert result["validation"]["unmatched_count"] == 1
    assert result["validation"]["leftover_component_count"] == 0
    assert "src/a.py::Typo" in result["warning"]

    validation = _read_validation_file(session)
    assert validation["unmatched_ids"] == ["src/a.py::Typo"]
    assert validation["leftover_component_ids"] == []


def test_unassigned_components_flagged(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B"]
    session = _make_session(store, tmp_path, ids)
    tree = {
        "core": {"components": ["src/a.py::A"]},
    }

    result = _save(tree, session, store)

    assert result["status"] == "saved"
    assert result["validation"]["unmatched_ids"] == []
    assert result["validation"]["leftover_component_count"] == 1
    assert "src/b.py::B" in result["validation"]["leftover_component_ids"]
    assert "src/b.py::B" in result["warning"]

    validation = _read_validation_file(session)
    assert validation["leftover_component_ids"] == ["src/b.py::B"]


def test_nested_children_validated(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B"]
    session = _make_session(store, tmp_path, ids)
    tree = {
        "root": {
            "components": ["src/a.py::A"],
            "children": {
                "child": {"components": ["src/b.py::B", "src/missing.py::X"]},
            },
        },
    }

    result = _save(tree, session, store)

    assert result["status"] == "saved"
    assert result["validation"]["unmatched_ids"] == ["src/missing.py::X"]
    assert result["validation"]["unmatched_count"] == 1
    assert result["validation"]["leftover_component_count"] == 0
    assert "src/missing.py::X" in result["warning"]


def test_multiple_unmatched_and_leftover(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    session = _make_session(store, tmp_path, ids)
    tree = {
        "core": {"components": ["src/a.py::A", "src/typo.py::T1", "src/typo.py::T2"]},
    }

    result = _save(tree, session, store)

    assert result["status"] == "saved"
    assert result["validation"]["unmatched_ids"] == [
        "src/typo.py::T1",
        "src/typo.py::T2",
    ]
    assert result["validation"]["unmatched_count"] == 2
    assert result["validation"]["leftover_component_count"] == 2
    assert result["validation"]["leftover_component_ids"] == [
        "src/b.py::B",
        "src/c.py::C",
    ]
    assert "src/typo.py::T1" in result["warning"]
    assert "src/b.py::B" in result["warning"]


def test_missing_session_errors(tmp_path):
    store = SessionStore()
    result = json.loads(handle_save_module_tree(
        {"session_id": "nope", "module_tree": {}},
        store,
    ))
    assert "error" in result
