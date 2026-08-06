"""Tests for save_module_tree validation of component ids.

Verifies that a module tree referencing unknown/stale component ids is
surfaced in the response (unmatched_ids) and that clustering-candidate
components (leaf nodes) left out of the tree are reported as an
informational coverage gap (leftover_component_ids), without breaking the
save itself. Leftover ids are computed against the leaf-node candidate set,
never the full index, because the cluster prompt intentionally excludes
non-essential components.
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
    leaf_nodes: list[str] | None = None,
) -> SessionState:
    components = {cid: _make_node(cid) for cid in component_ids}
    if leaf_nodes is None:
        leaf_nodes = list(component_ids)
    session = store.create(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        components=components,
        leaf_nodes=leaf_nodes,
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
    assert result["validation"]["unmatched_truncated"] is False
    assert result["validation"]["leftover_component_count"] == 0
    assert result["validation"]["leftover_truncated"] is False
    assert "warning" not in result
    assert "note" not in result

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
    assert "note" not in result

    validation = _read_validation_file(session)
    assert validation["unmatched_ids"] == ["src/a.py::Typo"]
    assert validation["leftover_component_ids"] == []


def test_unassigned_leaf_candidates_flagged_as_note(tmp_path):
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
    assert "warning" not in result
    assert "src/b.py::B" in result["note"]

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


def test_non_leaf_component_not_flagged_as_leftover(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B"]
    session = _make_session(store, tmp_path, ids, leaf_nodes=["src/a.py::A"])
    tree = {
        "core": {"components": ["src/a.py::A"]},
    }

    result = _save(tree, session, store)

    assert result["status"] == "saved"
    assert result["validation"]["unmatched_ids"] == []
    assert result["validation"]["leftover_component_count"] == 0
    assert "warning" not in result
    assert "note" not in result


def test_leftover_counts_only_leaf_candidates(tmp_path):
    store = SessionStore()
    ids = ["src/a.py::A", "src/b.py::B", "src/c.py::C"]
    session = _make_session(
        store,
        tmp_path,
        ids,
        leaf_nodes=["src/a.py::A", "src/b.py::B"],
    )
    tree = {
        "core": {"components": ["src/a.py::A"]},
    }

    result = _save(tree, session, store)

    assert result["validation"]["leftover_component_count"] == 1
    assert result["validation"]["leftover_component_ids"] == ["src/b.py::B"]
    assert "src/b.py::B" in result["note"]


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
    assert "src/b.py::B" in result["note"]


def test_id_lists_capped_in_response(tmp_path):
    store = SessionStore()
    leaf_ids = [f"src/l{i}.py::L{i}" for i in range(25)]
    session = _make_session(store, tmp_path, leaf_ids)
    typo_ids = [f"src/x{i}.py::X{i}" for i in range(25)]
    tree = {
        "mod": {"components": typo_ids},
    }

    result = _save(tree, session, store)

    validation = result["validation"]
    assert validation["unmatched_count"] == 25
    assert len(validation["unmatched_ids"]) == 20
    assert validation["unmatched_truncated"] is True
    assert validation["leftover_component_count"] == 25
    assert len(validation["leftover_component_ids"]) == 20
    assert validation["leftover_truncated"] is True
    assert "module_tree_validation.json" in result["warning"]
    assert "module_tree_validation.json" in result["note"]

    full = _read_validation_file(session)
    assert len(full["unmatched_ids"]) == 25
    assert len(full["leftover_component_ids"]) == 25


def test_missing_session_errors(tmp_path):
    store = SessionStore()
    result = json.loads(handle_save_module_tree(
        {"session_id": "nope", "module_tree": {}},
        store,
    ))
    assert "error" in result
