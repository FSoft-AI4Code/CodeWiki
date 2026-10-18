"""Follow-up questions: each turn's answer ends with a short ``<SUMMARY>``
(files read, key facts, open points), and later turns see the recent ones
instead of the full history (FastCode's multi-turn memory)."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict

MAX_SESSIONS = 100
MAX_TURNS = 6
TTL_SECONDS = 2 * 3600


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

    def _evict(self, now: float) -> None:
        for sid in [s for s, (seen, _) in self._sessions.items() if now - seen > TTL_SECONDS]:
            del self._sessions[sid]
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def get_or_create(
        self, session_id: str | None, keep_turns: int | None = None
    ) -> tuple[str, list[dict]]:
        """``keep_turns`` drops later turns first: the chat edited or retried
        an earlier question, so the answers after it no longer happened."""
        now = time.time()
        with self._lock:
            self._evict(now)
            if session_id and session_id in self._sessions:
                _, turns = self._sessions.pop(session_id)
            else:
                session_id = session_id or uuid.uuid4().hex[:12]
                turns = []
            if keep_turns is not None:
                del turns[max(0, keep_turns) :]
            self._sessions[session_id] = (now, turns)
            return session_id, list(turns)

    def add_turn(self, session_id: str, question: str, summary: str) -> None:
        with self._lock:
            seen, turns = self._sessions.get(session_id, (time.time(), []))
            turns.append({"question": question, "summary": summary})
            del turns[:-MAX_TURNS]
            self._sessions[session_id] = (time.time(), turns)

    def turn_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._sessions.get(session_id, (0.0, []))[1])


SESSIONS = SessionStore()
