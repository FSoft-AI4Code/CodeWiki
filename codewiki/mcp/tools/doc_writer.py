"""MCP tools: write_doc_file + edit_doc_file.

These tools create and edit markdown documentation files in the output
directory, with automatic Mermaid diagram validation after every write.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict

from codewiki.mcp.session import SessionState, SessionStore

logger = logging.getLogger(__name__)

# Max edit history entries per file (prevent unbounded memory growth)
_MAX_HISTORY_PER_FILE = 20


def _is_within(path: Path, base: Path) -> bool:
    """Return True if *path* resolves to somewhere inside *base*."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _safe_doc_path(session: SessionState, filename: str) -> Path | None:
    """Resolve *filename* within session.output_dir, guarding against traversal."""
    if not filename.endswith(".md"):
        filename += ".md"
    output_base = Path(session.output_dir).resolve()
    doc_path = (output_base / filename).resolve()
    if not _is_within(doc_path, output_base):
        return None
    return doc_path


def _ensure_parent_dirs(path: Path) -> None:
    """Create parent directories if they don't exist."""
    path.parent.mkdir(parents=True, exist_ok=True)


async def _validate_mermaid(file_path: str, relative_path: str) -> str:
    """Run Mermaid validation and return the result string."""
    try:
        from codewiki.src.be.utils import validate_mermaid_diagrams
        return await validate_mermaid_diagrams(file_path, relative_path)
    except Exception as e:
        return f"Mermaid validation skipped: {e}"


def _save_history(session: SessionState, doc_path: Path, content: str) -> None:
    """Append *content* to edit history for *doc_path*, capped at _MAX_HISTORY_PER_FILE."""
    history = session.registry.get("file_history")
    if history is None:
        history = {}
    elif isinstance(history, str):
        history = json.loads(history)
    key = str(doc_path)
    entry = history.setdefault(key, [])
    entry.append(content)
    # Trim to last N entries
    if len(entry) > _MAX_HISTORY_PER_FILE:
        del entry[: len(entry) - _MAX_HISTORY_PER_FILE]
    session.registry["file_history"] = history  # keep as native dict


async def handle_write_doc_file(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Create a new documentation file in the output directory."""
    session_id = arguments["session_id"]
    session = store.get(session_id)
    if session is None:
        return json.dumps({"error": f"Session {session_id} not found or expired."})

    filename = arguments["filename"]
    doc_path = _safe_doc_path(session, filename)
    if doc_path is None:
        return json.dumps({"error": "Filename escapes output directory."})

    content = arguments["content"]

    await asyncio.to_thread(_ensure_parent_dirs, doc_path)

    if await asyncio.to_thread(doc_path.exists):
        return json.dumps({
            "error": f"File already exists: {filename}. Use edit_doc_file to modify it."
        })

    await asyncio.to_thread(doc_path.write_text, content, "utf-8")
    session.docs_written += 1

    # Mermaid validation
    mermaid_result = await _validate_mermaid(str(doc_path), filename)

    result = {
        "status": "created",
        "path": str(doc_path),
        "filename": filename,
        "lines": content.count("\n") + 1,
        "mermaid_validation": mermaid_result,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


async def handle_edit_doc_file(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Edit an existing documentation file (str_replace, insert, or undo)."""
    session_id = arguments["session_id"]
    session = store.get(session_id)
    if session is None:
        return json.dumps({"error": f"Session {session_id} not found or expired."})

    filename = arguments["filename"]
    doc_path = _safe_doc_path(session, filename)
    if doc_path is None:
        return json.dumps({"error": "Filename escapes output directory."})

    command = arguments["command"]

    if command == "undo":
        # Undo via registry history
        history = session.registry.get("file_history", {})
        if isinstance(history, str):
            history = json.loads(history)
        path_history = history.get(str(doc_path), [])
        if not path_history:
            return json.dumps({"error": f"No edit history found for {filename}."})
        old_content = path_history.pop()
        history[str(doc_path)] = path_history
        session.registry["file_history"] = history
        await asyncio.to_thread(doc_path.write_text, old_content, "utf-8")

        # Validate Mermaid after undo
        mermaid_result = await _validate_mermaid(str(doc_path), filename)
        return json.dumps({
            "status": "undone",
            "filename": filename,
            "mermaid_validation": mermaid_result,
        }, ensure_ascii=False)

    if not await asyncio.to_thread(doc_path.exists):
        return json.dumps({"error": f"File not found: {filename}. Use write_doc_file to create it."})

    current_content = await asyncio.to_thread(doc_path.read_text, "utf-8")

    if command == "str_replace":
        old_str = arguments.get("old_str")
        new_str = arguments.get("new_str", "")
        if old_str is None:
            return json.dumps({"error": "old_str is required for str_replace."})

        occurrences = current_content.count(old_str)
        if occurrences == 0:
            return json.dumps({"error": f"old_str not found in {filename}."})
        if occurrences > 1:
            return json.dumps({"error": f"old_str appears {occurrences} times in {filename}. Make it unique."})

        new_content = current_content.replace(old_str, new_str, 1)
        # Save history only for edits that actually happen, so undo never
        # pops a no-op entry left behind by a failed/rejected command.
        _save_history(session, doc_path, current_content)
        await asyncio.to_thread(doc_path.write_text, new_content, "utf-8")

        # Snippet around the edit
        replacement_line = current_content.split(old_str)[0].count("\n")
        lines = new_content.split("\n")
        start = max(0, replacement_line - 4)
        end = min(len(lines), start + new_str.count("\n") + 9)
        snippet = "\n".join(f"{i + 1:6}\t{lines[i]}" for i in range(start, end))

    elif command == "insert":
        insert_line = arguments.get("insert_line", 0)
        new_str = arguments.get("new_str", "")
        if not new_str:
            return json.dumps({"error": "new_str is required for insert."})

        lines = current_content.split("\n")
        insert_line = max(0, min(insert_line, len(lines)))
        new_str_lines = new_str.split("\n")
        lines = lines[:insert_line] + new_str_lines + lines[insert_line:]
        new_content = "\n".join(lines)
        _save_history(session, doc_path, current_content)
        await asyncio.to_thread(doc_path.write_text, new_content, "utf-8")

        start = max(0, insert_line - 4)
        end = min(len(lines), start + len(new_str_lines) + 8)
        snippet = "\n".join(f"{i + 1:6}\t{lines[i]}" for i in range(start, end))

    else:
        return json.dumps({"error": f"Unknown command: {command}. Use str_replace, insert, or undo."})

    session.docs_written += 1

    # Mermaid validation
    mermaid_result = await _validate_mermaid(str(doc_path), filename)

    result = {
        "status": "edited",
        "command": command,
        "filename": filename,
        "snippet": snippet,
        "mermaid_validation": mermaid_result,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
