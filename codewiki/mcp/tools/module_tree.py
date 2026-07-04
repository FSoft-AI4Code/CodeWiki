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
        "hint": (
            "Read the processing_order.json file for the leaf-first generation order. "
            "Process leaf modules first (is_leaf=true), then parent modules. "
            "For each leaf module: get_prompt('system_leaf') + read_code_components + write_doc_file. "
            "For each parent module: get_prompt('overview_module') + write_doc_file."
        ),
    }
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
