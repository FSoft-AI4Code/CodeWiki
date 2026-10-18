"""Session data persistence — writes raw session data to disk."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from codewiki._vendor.caw.models import TextBlock, ThinkingBlock, ToolUse, Trajectory, Turn

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class JsonlWriter:
    """Append-only JSONL writer with file locking for concurrent safety.

    When *subagent* is set, every entry is tagged with ``"subagent": name``
    so readers can distinguish parent vs. subagent events.
    """

    def __init__(self, path: str | Path, *, subagent: str | None = None) -> None:
        self._path = Path(path)
        self._subagent = subagent

    def append(self, entry: dict[str, Any]) -> None:
        """Append a single JSON object as one line (file-locked)."""
        if self._subagent:
            entry = {**entry, "subagent": self._subagent}
        line = json.dumps(entry) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            if sys.platform != "win32":
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(line)
                return
            # Lock byte 0 as a mutex; O_APPEND still writes at the end.
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                f.write(line)
                f.flush()
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def write_metadata(self, trajectory: Trajectory) -> None:
        """Write a metadata entry (typically once at session start)."""
        self.append(
            {
                "type": "metadata",
                "session_id": trajectory.session_id,
                "created_at": trajectory.created_at,
                "agent": trajectory.agent,
                "model": trajectory.model,
                "system_prompt": trajectory.system_prompt,
                "reasoning": trajectory.reasoning,
                "mcp_servers": [
                    {"name": s.name, "command": s.command, "args": s.args, "env": s.env, "url": s.url}
                    for s in trajectory.mcp_servers
                ],
                "metadata": trajectory.metadata,
            }
        )

    def write_turn_events(self, turn: Turn, turn_index: int) -> None:
        """Write per-event JSONL lines for a completed turn."""
        # User message
        self.append({"type": "user", "message": turn.input, "turn_index": turn_index})

        # Content blocks — mirrors Display event order
        for block in turn.output:
            if isinstance(block, ThinkingBlock):
                self.append(
                    {
                        "type": "thinking",
                        "text": block.text,
                        "turn_index": turn_index,
                    }
                )
            elif isinstance(block, TextBlock):
                self.append(
                    {
                        "type": "text",
                        "text": block.text,
                        "turn_index": turn_index,
                    }
                )
            elif isinstance(block, ToolUse):
                self.append(
                    {
                        "type": "tool_call",
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.arguments,
                        "turn_index": turn_index,
                    }
                )
                result_entry: dict[str, Any] = {
                    "type": "tool_result",
                    "id": block.id,
                    "name": block.name,
                    "output": block.output,
                    "is_error": block.is_error,
                    "turn_index": turn_index,
                }
                if block.subagent_trajectory:
                    result_entry["subagent_trajectory"] = block.subagent_trajectory.to_dict()
                self.append(result_entry)

        # Turn-end stats
        self.append(
            {
                "type": "turn_end",
                "turn_index": turn_index,
                "usage": turn.usage.to_dict(),
                "duration_ms": turn.duration_ms,
            }
        )


class SessionStore:
    """Persists session data to a directory on disk.

    Layout:

        <data_dir>/sessions/<session_id>/
            traj.jsonl          # incremental append-only event log
            trajectory.json     # full trajectory, overwritten after each turn
            turns/
                000_input.txt
                000_raw_output.jsonl
    """

    def __init__(self, data_dir: str | Path, session_id: str, resume: bool = False) -> None:
        self._session_dir = Path(data_dir) / "sessions" / session_id
        self._turns_dir = self._session_dir / "turns"
        self._turns_dir.mkdir(parents=True, exist_ok=True)
        # On resume, continue numbering after the existing turn files so we
        # append to — rather than overwrite — the original session's record.
        self._turn_counter = self._next_turn_index() if resume else 0
        self._jsonl = JsonlWriter(self._session_dir / "traj.jsonl")

    def _next_turn_index(self) -> int:
        """One past the highest ``NNN_input.txt`` index already on disk."""
        indices = [int(p.name[:3]) for p in self._turns_dir.glob("*_input.txt") if p.name[:3].isdigit()]
        return max(indices) + 1 if indices else 0

    @property
    def session_dir(self) -> Path:
        """Path to this session's directory."""
        return self._session_dir

    @property
    def jsonl_path(self) -> Path:
        """Path to the traj.jsonl file."""
        return self._jsonl._path

    # ------------------------------------------------------------------
    # JSONL delegation
    # ------------------------------------------------------------------

    def write_metadata(self, trajectory: Trajectory) -> None:
        """Append a metadata entry to traj.jsonl (called once at session start)."""
        self._jsonl.write_metadata(trajectory)

    # ------------------------------------------------------------------
    # Trajectory
    # ------------------------------------------------------------------

    def _save_trajectory(self, trajectory: Trajectory) -> None:
        """Overwrite trajectory.json with the full trajectory."""
        path = self._session_dir / "trajectory.json"
        path.write_text(json.dumps(trajectory.to_dict(), indent=2) + "\n", encoding="utf-8")

    def finalize(self, trajectory: Trajectory) -> None:
        """Write trajectory.json with the complete session record."""
        self._save_trajectory(trajectory)

    # ------------------------------------------------------------------
    # Turn files
    # ------------------------------------------------------------------

    def append_turn(self, turn: Turn, trajectory: Trajectory, raw_output: str | None = None) -> None:
        """Record a completed turn: event JSONL lines, trajectory snapshot, and raw files."""
        prefix = f"{self._turn_counter:03d}"

        # Raw turn files
        input_path = self._turns_dir / f"{prefix}_input.txt"
        input_path.write_text(turn.input, encoding="utf-8")

        if raw_output is not None:
            output_path = self._turns_dir / f"{prefix}_raw_output.jsonl"
            output_path.write_text(raw_output, encoding="utf-8")

        # Per-event JSONL lines
        self._jsonl.write_turn_events(turn, self._turn_counter)

        # Full trajectory snapshot
        self._save_trajectory(trajectory)

        self._turn_counter += 1
