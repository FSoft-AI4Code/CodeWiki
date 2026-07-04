"""Session state management for the CodeWiki MCP Server.

Each ``analyze_repo`` call creates a new session that caches the analysis
results (components, leaf nodes, etc.) in memory.  Subsequent tool calls
reference the session by ``session_id`` to read code, write docs, and
manage the module tree without re-parsing the repository.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from codewiki.src.be.dependency_analyzer.models.core import Node

if TYPE_CHECKING:
    from codewiki.mcp.workspace import SessionWorkspace


# Sessions auto-expire after this many seconds of inactivity.
_SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours

# Maximum concurrent sessions to prevent unbounded memory growth.
_MAX_SESSIONS = 10


@dataclass
class SessionState:
    """Mutable state shared across all MCP tool calls within a session."""

    session_id: str
    repo_path: str
    output_dir: str
    components: Dict[str, Node]
    leaf_nodes: List[str]
    module_tree: Dict[str, Any] = field(default_factory=dict)
    registry: Dict[str, Any] = field(default_factory=dict)
    workspace: Optional[SessionWorkspace] = field(default=None)
    # HEAD commit at analyze_repo time — the incremental-update baseline.
    # close_session must record this commit (not the HEAD at close time),
    # otherwise commits made mid-session are baselined away undocumented.
    analyzed_commit: Optional[str] = None
    # Number of successful write_doc_file/edit_doc_file operations; used to
    # skip the metadata.json baseline update when a session wrote no docs.
    docs_written: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    def touch(self) -> None:
        """Update the last-accessed timestamp."""
        self.last_accessed = time.time()

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_accessed) > _SESSION_TTL_SECONDS


class SessionStore:
    """In-memory store for all active MCP sessions (thread-safe)."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def create(
        self,
        repo_path: str,
        output_dir: str,
        components: Dict[str, Node],
        leaf_nodes: List[str],
        workspace: Optional[SessionWorkspace] = None,
    ) -> SessionState:
        """Create a new session and return it."""
        with self._lock:
            self._purge_expired_locked()
            # Evict oldest if at capacity
            if len(self._sessions) >= _MAX_SESSIONS:
                oldest_id = min(
                    self._sessions,
                    key=lambda sid: self._sessions[sid].last_accessed,
                )
                evicted = self._sessions[oldest_id]
                if evicted.workspace is not None:
                    evicted.workspace.cleanup()
                del self._sessions[oldest_id]
            session_id = uuid.uuid4().hex[:12]
            # Ensure no collision
            while session_id in self._sessions:
                session_id = uuid.uuid4().hex[:12]
            state = SessionState(
                session_id=session_id,
                repo_path=repo_path,
                output_dir=output_dir,
                components=components,
                leaf_nodes=leaf_nodes,
                workspace=workspace,
            )
            self._sessions[session_id] = state
            return state

    def get(self, session_id: str) -> Optional[SessionState]:
        """Return the session or ``None`` if not found / expired."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            if state.is_expired:
                if state.workspace is not None:
                    state.workspace.cleanup()
                del self._sessions[session_id]
                return None
            state.touch()
            return state

    def remove(self, session_id: str) -> bool:
        """Remove a session.  Returns True if it existed."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def _purge_expired_locked(self) -> None:
        """Remove all expired sessions.  Caller must hold _lock."""
        expired = [sid for sid, s in self._sessions.items() if s.is_expired]
        for sid in expired:
            state = self._sessions[sid]
            if state.workspace is not None:
                state.workspace.cleanup()
            del self._sessions[sid]
