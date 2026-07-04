"""Session file workspace -- write large analysis artifacts to disk.

Instead of transmitting bulky data through the MCP stdio channel, the
server writes analysis results (component index, leaf nodes, source code,
etc.) to a per-session directory on disk.  The IDE agent then reads these
files directly using its own file-access capabilities.

Directory layout (relative to ``repo_path``)::

    .codewiki/sessions/{session_id}/
        component_index.json
        leaf_nodes.json
        languages.json
        changes.json
        summary.json
        processing_order.json
        sources/
            {sanitized_component_id}.src
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Base directory under repo_path
_WORKSPACE_REL = Path(".codewiki") / "sessions"


def _safe_filename(component_id: str) -> str:
    """Sanitize a component ID for use as a filename.

    Component IDs look like ``src/main.py::MyClass``.  We replace any
    character that is not a word char, hyphen, or dot with ``__``.
    A short hash suffix is appended to prevent collisions when different
    component IDs sanitize to the same string (e.g. ``src/a-b.py`` vs
    ``src/a_b.py``).

    The sanitized part is truncated so the filename stays under the
    common 255-byte NAME_MAX (separator chars expand to two chars each,
    so deep paths overflow it easily); the hash suffix keeps truncated
    names unique.
    """
    sanitized = re.sub(r"[^\w\-.]", "__", component_id)[:180]
    hash_suffix = hashlib.sha1(component_id.encode()).hexdigest()[:8]
    return f"{sanitized}_{hash_suffix}.src"


class SessionWorkspace:
    """Manages the on-disk workspace for a single MCP session."""

    def __init__(self, repo_path: Path, session_id: str) -> None:
        self.root = repo_path / _WORKSPACE_REL / session_id
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sources").mkdir(exist_ok=True)
        logger.debug("Workspace created at %s", self.root)

    # -- writers ----------------------------------------------------------

    def write_json(self, name: str, data: Any) -> Path:
        """Write *data* as pretty-printed JSON and return the file path."""
        p = self.root / name
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def write_component_source(
        self,
        component_id: str,
        source: str,
        language: str = "",
    ) -> Path:
        """Write a single component's source code to the ``sources/`` dir."""
        p = self.root / "sources" / _safe_filename(component_id)
        header = f"// Component: {component_id}\n// Language: {language}\n"
        p.write_text(header + source, encoding="utf-8")
        return p

    # -- readers ----------------------------------------------------------

    def read_json(self, name: str) -> Any:
        """Read a JSON file from the workspace.  Returns ``None`` if missing."""
        p = self.root / name
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    # -- cleanup ----------------------------------------------------------

    def cleanup(self) -> None:
        """Remove the session directory and try to prune empty parents."""
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        # Walk up and remove empty parent directories
        try:
            sessions_dir = self.root.parent  # .codewiki/sessions
            if sessions_dir.exists() and not any(sessions_dir.iterdir()):
                sessions_dir.rmdir()
                base_dir = sessions_dir.parent  # .codewiki
                if base_dir.exists() and not any(base_dir.iterdir()):
                    base_dir.rmdir()
        except OSError:
            pass
        logger.debug("Workspace cleaned up: %s", self.root)
