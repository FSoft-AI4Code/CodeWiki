"""MCP tools: save_module_tree + get_processing_order.

The IDE agent decides how to group components into modules (clustering)
using its own LLM.  These tools persist that decision and compute the
leaf-first processing order for documentation generation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from codewiki.mcp.session import SessionState, SessionStore
from codewiki.src.config import FIRST_MODULE_TREE_FILENAME, MODULE_TREE_FILENAME

logger = logging.getLogger(__name__)


def _get_processing_order(module_tree: Dict[str, Any], parent_path: List[str] | None = None) -> List[Dict[str, Any]]:
    """Compute leaf-first processing order from a module tree.

    Returns a list of dicts with module path, name, leaf status, and
    component/children info.
    """
    if parent_path is None:
        parent_path = []
    order: List[Dict[str, Any]] = []

    def _collect(tree: Dict[str, Any], path: List[str]) -> None:
        for module_name, module_info in tree.items():
            current_path = path + [module_name]
            children = module_info.get("children", {})
            has_children = isinstance(children, dict) and len(children) > 0

            if has_children:
                _collect(children, current_path)
                order.append({
                    "module": module_name,
                    "path": current_path,
                    "is_leaf": False,
                    "children": list(children.keys()),
                    "components": module_info.get("components", []),
                })
            else:
                order.append({
                    "module": module_name,
                    "path": current_path,
                    "is_leaf": True,
                    "components": module_info.get("components", []),
                })

    _collect(module_tree, parent_path)
    return order


def _collect_component_ids(module_tree: Dict[str, Any]) -> set[str]:
    """Return the set of all component ids referenced across the module tree.

    Walks every module (and nested ``children``) and collects the entries of
    each module's ``components`` list.
    """
    ids: set[str] = set()

    def _walk(tree: Dict[str, Any]) -> None:
        for module_info in tree.values():
            ids.update(module_info.get("components", []) or [])
            children = module_info.get("children", {})
            if isinstance(children, dict) and children:
                _walk(children)

    _walk(module_tree)
    return ids


def _validate_module_tree(
    module_tree: Dict[str, Any],
    known_ids: set[str],
) -> Tuple[List[str], List[str]]:
    """Check the tree's component ids against the analysis index.

    Returns ``(unmatched_ids, leftover_ids)``:
      * ``unmatched_ids``: ids referenced by the tree that do not exist in the
        index (typos / drift) -- they will be silently omitted from docs.
      * ``leftover_ids``: ids that exist in the index but are not assigned to
        any module (a coverage gap -- they get no module doc).
    """
    assigned = _collect_component_ids(module_tree)
    unmatched = sorted(assigned - known_ids)
    leftover = sorted(known_ids - assigned)
    return unmatched, leftover


def handle_save_module_tree(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Persist the IDE agent's clustering result as the module tree."""
    session_id = arguments["session_id"]
    session = store.get(session_id)
    if session is None:
        return json.dumps({"error": f"Session {session_id} not found or expired."})

    module_tree = arguments["module_tree"]
    output_dir = session.output_dir
    known_ids = set(session.components.keys())

    # Save both immutable snapshot and mutable working copy
    first_path = os.path.join(output_dir, FIRST_MODULE_TREE_FILENAME)
    working_path = os.path.join(output_dir, MODULE_TREE_FILENAME)

    os.makedirs(output_dir, exist_ok=True)

    with open(first_path, "w", encoding="utf-8") as f:
        json.dump(module_tree, f, indent=2, ensure_ascii=False)
    with open(working_path, "w", encoding="utf-8") as f:
        json.dump(module_tree, f, indent=2, ensure_ascii=False)

    # Cache in session
    session.module_tree = module_tree

    # Validate the tree against the analysis component index so orphaned /
    # stale ids surface loudly instead of being silently dropped from docs.
    unmatched_ids, leftover_ids = _validate_module_tree(module_tree, known_ids)
    validation = {
        "unmatched_ids": unmatched_ids,
        "unmatched_count": len(unmatched_ids),
        "leftover_component_ids": leftover_ids,
        "leftover_component_count": len(leftover_ids),
    }
    if session.workspace is not None:
        session.workspace.write_json("module_tree_validation.json", validation)

    if unmatched_ids or leftover_ids:
        warnings: List[str] = []
        if unmatched_ids:
            warnings.append(
                f"{len(unmatched_ids)} component id(s) in the module tree do not "
                f"exist in the analysis index and will be omitted from docs: "
                f"{unmatched_ids}"
            )
        if leftover_ids:
            warnings.append(
                f"{len(leftover_ids)} indexed component(s) are assigned to no "
                f"module and will receive no documentation: {leftover_ids}"
            )
        warning = " ".join(warnings)
        logger.warning(
            "save_module_tree for session %s: %s",
            session_id,
            warning,
        )
    else:
        warning = ""

    # Compute processing order and write to workspace file
    order = _get_processing_order(module_tree)
    order_file = None
    if session.workspace is not None:
        order_path = session.workspace.write_json("processing_order.json", order)
        order_file = str(order_path)

    result = {
        "status": "saved",
        "module_count": len(module_tree),
        "tree_path": working_path,
        "first_tree_path": first_path,
        "processing_order_file": order_file,
        "validation": validation,
        "hint": (
            "Read the processing_order.json file for the leaf-first generation order. "
            "Process leaf modules first (is_leaf=true), then parent modules. "
            "For each leaf module: get_prompt('system_leaf') + read_code_components + write_doc_file. "
            "For each parent module: get_prompt('overview_module') + write_doc_file."
        ),
    }
    if warning:
        result["warning"] = warning
    return json.dumps(result, indent=2, ensure_ascii=False)


def handle_get_processing_order(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Write the leaf-first processing order to a workspace file and return its path."""
    session_id = arguments["session_id"]
    session = store.get(session_id)
    if session is None:
        return json.dumps({"error": f"Session {session_id} not found or expired."})

    # Try session cache first, then disk
    module_tree = session.module_tree
    if not module_tree:
        tree_path = os.path.join(session.output_dir, MODULE_TREE_FILENAME)
        if os.path.exists(tree_path):
            with open(tree_path, encoding="utf-8") as f:
                module_tree = json.load(f)
            session.module_tree = module_tree
        else:
            return json.dumps({
                "error": "Module tree not found. Call save_module_tree first."
            })

    order = _get_processing_order(module_tree)

    # Write to workspace file
    order_file = None
    if session.workspace is not None:
        order_path = session.workspace.write_json("processing_order.json", order)
        order_file = str(order_path)

    result = {
        "session_id": session_id,
        "module_count": len(module_tree),
        "processing_order_file": order_file,
        "hint": "Read the processing_order.json file for the full leaf-first order.",
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
