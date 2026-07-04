"""MCP tool: read_code_components — write component source code to disk.

Instead of transmitting source code through the MCP stdio channel (which
required aggressive truncation), this tool writes complete, untruncated
source files to the session workspace.  The IDE agent then reads them
directly from disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from codewiki.mcp.session import SessionStore

logger = logging.getLogger(__name__)


def handle_read_code_components(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Write the source code for given component IDs to workspace files.

    Returns a compact JSON with file paths — no source code inline.
    """
    session_id = arguments["session_id"]
    session = store.get(session_id)
    if session is None:
        return json.dumps({"error": f"Session {session_id} not found or expired."})

    if session.workspace is None:
        return json.dumps({"error": "Session workspace not initialized."})

    component_ids: List[str] = arguments["component_ids"]
    components = session.components
    workspace = session.workspace

    written_files: Dict[str, str] = {}  # filename -> component_id
    not_found: List[str] = []

    for cid in component_ids:
        node = components.get(cid)
        if node is None:
            not_found.append(cid)
            continue
        lang = getattr(node, "language", "")
        source = (getattr(node, "source_code", None) or "").strip()
        file_path = workspace.write_component_source(cid, source, lang)
        written_files[file_path.name] = cid

    result = {
        "written": len(written_files),
        "not_found_count": len(not_found),
        "not_found": not_found,
        "source_dir": str(workspace.root / "sources"),
        "files": written_files,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
