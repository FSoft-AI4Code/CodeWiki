"""``claudep`` provider — subscription-backed Claude Code via the interactive TUI.

This is a variant of the :mod:`caw.providers.claude_code` provider that does
**not** call ``claude -p``.  Instead it drives the *interactive* ``claude`` TUI
under a pseudo-TTY, waits for the turn to finish by tailing Claude Code's
canonical session JSONL (``~/.claude/projects/**/<session-id>.jsonl``), and
feeds those JSONL events through the exact same parsing/display machinery the
``-p`` provider uses.

Why this exists: in some environments interactive Claude Code works with the
local subscription login, while programmatic ``claude -p`` usage is capped,
billed differently, or unavailable.  The technique mirrors the third-party
``claude-p`` project, but the code is shared with the in-tree ``claude_code``
provider rather than depending on it.

Almost everything is inherited from ``claude_code``: block parsing, tool-result
pairing, display/logger fan-out, MCP config, tool restrictions, model
resolution, and resume semantics.  Only ``send`` differs (pty + JSONL tail
instead of an ``-p`` pipe), plus a usage-limit hook that reads the rendered
terminal because the TUI surfaces limits there rather than in a result event.
"""

from __future__ import annotations

import glob
import json
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from codewiki._vendor.caw.display import Display, get_global_display
from codewiki._vendor.caw.logger import log_metadata, log_turn_end
from codewiki._vendor.caw.models import ContentBlock, MCPServer, ToolUse, Trajectory, Turn, UsageStats
from codewiki._vendor.caw.pricing import compute_cost
from codewiki._vendor.caw.providers.claude_code import (
    ClaudeCodeProvider,
    ClaudeCodeSession,
    _register_process,
    _unregister_process,
    detect_usage_limit,
)

# Stop reasons that mean "more is coming this turn" — anything else (end_turn,
# max_tokens, stop_sequence, refusal, …) marks the turn complete.
_NON_TERMINAL_STOP_REASONS = {"tool_use", "pause_turn"}

# Provider/auth env vars that, if present, would route the headless ``claude``
# away from the interactive subscription backend.  Stripped by default.
_SUBSCRIPTION_ENV_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)

# When caw itself runs inside a Claude Code session (e.g. launched from a Claude
# Code Bash tool), the parent injects these.  A child ``claude`` that inherits
# them attaches to the *parent's* session and ignores our ``--session-id`` — so
# the session JSONL we poll for never appears.  Always stripped so our
# deterministic ``--session-id`` is honored regardless of how caw was launched.
_NESTED_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")

_DEFAULT_TIMEOUT_SEC = 3600.0
_JSONL_POLL_SEC = 0.4


def _clean_terminal(text: str) -> str:
    text = _OSC_RE.sub("", text)
    text = _ANSI_RE.sub("", text)
    return text.replace("\r", "").replace(" ", " ")


def _classify_interactive_block(transcript: str) -> str | None:
    """Detect TUI states that will never produce an assistant answer."""
    low = _clean_terminal(transcript).lower()
    compact = re.sub(r"[^a-z0-9]+", "", low)
    if "failed to authenticate" in low or "api error: 403" in low or "pleaserunlogin" in compact:
        return "auth_blocked"
    if "hit your limit" in low or "out of usage" in low or "usage limit" in low:
        return "rate_limit"
    if ("do you trust" in low and "folder" in low) or "workspacetrust" in compact:
        return "workspace_trust_blocked"
    return None


class ClaudePSession(ClaudeCodeSession):
    """Subscription-backed Claude Code session driven through the interactive TUI."""

    _AGENT_NAME = "claudep"

    def __init__(
        self,
        mcp_servers: list[MCPServer],
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        disallowed_tools: list[str] | None = None,
        reasoning: str | None = None,
        cwd: str | None = None,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        strip_provider_env: bool = True,
    ) -> None:
        super().__init__(
            mcp_servers=mcp_servers,
            model=model,
            system_prompt=system_prompt,
            session_id=session_id,
            disallowed_tools=disallowed_tools,
            reasoning=reasoning,
            cwd=cwd,
        )
        self._timeout_sec = timeout_sec
        self._strip_provider_env = strip_provider_env

    # ------------------------------------------------------------------
    # Core send (interactive pty + session-JSONL tail)
    # ------------------------------------------------------------------

    def send(self, message: str) -> Turn:
        display = get_global_display()
        self._emit_send_preamble(message, display)

        cmd = self._build_command(message)

        blocks: list[ContentBlock] = []
        tool_blocks: dict[str, ToolUse] = {}

        # Only events appended after this offset belong to the current turn; the
        # session JSONL accumulates every turn of the conversation.
        start_pos = self._jsonl_size()

        raw, usage, duration_ms = self._drive_tui(cmd, start_pos, blocks, tool_blocks, display)
        usage.cost_usd = compute_cost(self._AGENT_NAME, self._model or "", usage)

        self._last_raw_output = raw
        self._has_sent = True

        turn = Turn(input=message, output=blocks, usage=usage, duration_ms=duration_ms)

        if display:
            display.on_turn_end(turn.result, usage, duration_ms)
        log_turn_end(self._logger, usage, duration_ms)

        self._turns.append(turn)
        self._total_usage = self._total_usage + turn.usage
        self._total_duration_ms += turn.duration_ms
        return turn

    # ------------------------------------------------------------------
    # Usage-limit detection — the TUI shows limits in the rendered terminal,
    # not in an assistant message, so fall back to the captured transcript.
    # ------------------------------------------------------------------

    def detect_usage_limit(self, turn: Turn) -> int | None:
        hit = detect_usage_limit(turn.result)
        if hit is not None:
            return hit
        if self._last_raw_output:
            return detect_usage_limit(_clean_terminal(self._last_raw_output))
        return None

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def _build_command(self, message: str) -> list[str]:
        cmd = ["claude"]
        if not self._has_sent:
            cmd += ["--session-id", self._session_id]
        else:
            cmd += ["--resume", self._session_id]

        # Run tools without interactive approval prompts (which would otherwise
        # block the turn forever under automation).
        cmd += ["--dangerously-skip-permissions"]

        if self._model:
            cmd += ["--model", self._model]
        if self._reasoning:
            cmd += ["--effort", self._reasoning]
        if self._disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self._disallowed_tools)]
        if not self._has_sent and self._system_prompt:
            cmd += ["--system-prompt", self._system_prompt]

        mcp_path = self._ensure_mcp_config()
        if mcp_path:
            cmd += ["--mcp-config", mcp_path]

        # ``--`` terminates option parsing so the prompt is always taken as the
        # positional argument.  Without it, a preceding variadic flag such as
        # ``--disallowedTools <tools...>`` greedily swallows the prompt as tool
        # names.  Interactive claude submits the positional prompt on startup.
        cmd += ["--", message]
        return cmd

    # ------------------------------------------------------------------
    # Session-JSONL helpers
    # ------------------------------------------------------------------

    def _jsonl_path(self) -> Path | None:
        pattern = str(Path.home() / ".claude" / "projects" / "**" / f"{self._session_id}.jsonl")
        paths = [Path(p) for p in glob.glob(pattern, recursive=True)]
        if not paths:
            return None
        return max(paths, key=lambda p: p.stat().st_mtime)

    def _jsonl_size(self) -> int:
        path = self._jsonl_path()
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _drain_jsonl(
        self,
        path: Path,
        pos: int,
        blocks: list[ContentBlock],
        tool_blocks: dict[str, ToolUse],
        display: Display | None,
    ) -> tuple[int, UsageStats, bool]:
        """Process complete JSONL lines appended past *pos*.

        Returns the advanced byte offset, the usage summed over assistant
        messages seen in this drain, and whether a terminal assistant message
        (turn complete) was observed.
        """
        usage = UsageStats()
        terminal = False
        try:
            size = path.stat().st_size
        except OSError:
            return pos, usage, terminal
        if size <= pos:
            return pos, usage, terminal
        try:
            with path.open("rb") as f:
                f.seek(pos)
                data = f.read(size - pos)
        except OSError:
            return pos, usage, terminal

        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return pos, usage, terminal  # no complete line yet
        new_pos = pos + last_nl + 1

        for raw_line in data[:last_nl].split(b"\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype = event.get("type")
            if etype == "assistant":
                msg = event.get("message", {})
                if not self._model and msg.get("model"):
                    self._model = msg["model"]
                    if display:
                        display.on_metadata(model=self._model)
                    log_metadata(self._logger, model=self._model)
                self._emit_blocks(self._parse_assistant_blocks(event), blocks, tool_blocks, display)
                if isinstance(msg.get("usage"), dict):
                    usage = usage + self._usage_from_message(msg)
                stop_reason = msg.get("stop_reason")
                if stop_reason is not None and stop_reason not in _NON_TERMINAL_STOP_REASONS:
                    terminal = True
            elif etype == "user":
                self._pair_tool_results(event, tool_blocks, display)
            if self._step_callback and blocks:
                self._step_callback(list(blocks))

        return new_pos, usage, terminal

    @staticmethod
    def _usage_from_message(msg: dict[str, Any]) -> UsageStats:
        u = msg.get("usage", {})
        return UsageStats(
            input_tokens=u.get("input_tokens", 0) or 0,
            output_tokens=u.get("output_tokens", 0) or 0,
            cache_read_tokens=u.get("cache_read_input_tokens", 0) or 0,
            cache_write_tokens=u.get("cache_creation_input_tokens", 0) or 0,
        )

    # ------------------------------------------------------------------
    # PTY drive loop
    # ------------------------------------------------------------------

    def _drive_tui(
        self,
        cmd: list[str],
        start_pos: int,
        blocks: list[ContentBlock],
        tool_blocks: dict[str, ToolUse],
        display: Display | None,
    ) -> tuple[str, UsageStats, int]:
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        for name in _NESTED_SESSION_ENV:
            env.pop(name, None)
        if self._strip_provider_env:
            for name in _SUBSCRIPTION_ENV_OVERRIDES:
                env.pop(name, None)

        import pty  # Unix-only; imported here so caw still loads on Windows

        master, slave = pty.openpty()
        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=self._cwd or os.getcwd()
            )
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            raise RuntimeError("claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code")
        os.close(slave)
        _register_process(proc)

        raw = bytearray()
        usage = UsageStats()
        pos = start_pos
        path = self._jsonl_path()
        last_poll = 0.0

        def _drain() -> bool:
            nonlocal pos, usage, path
            if path is None:
                path = self._jsonl_path()
            if path is None:
                return False
            pos, delta, terminal = self._drain_jsonl(path, pos, blocks, tool_blocks, display)
            usage = usage + delta
            return terminal

        try:
            while time.time() - start < self._timeout_sec:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b""
                    if chunk:
                        raw.extend(chunk)

                now = time.time()
                if now - last_poll >= _JSONL_POLL_SEC:
                    last_poll = now
                    if _drain():
                        break
                    if _classify_interactive_block(raw.decode("utf-8", "replace")):
                        break

                if proc.poll() is not None:
                    break
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            _unregister_process(proc)
            os.close(master)

        # Final drain to capture anything written between the last poll and exit.
        _drain()

        duration_ms = int((time.time() - start) * 1000)
        return raw.decode("utf-8", "replace"), usage, duration_ms


class ClaudePProvider(ClaudeCodeProvider):
    """Provider that drives the interactive ``claude`` TUI (subscription backend)."""

    # This backend drives a TUI rather than a headless run, so it carries two
    # options of its own on top of what claude_code accepts.
    EXTRA_SESSION_OPTIONS = ClaudeCodeProvider.EXTRA_SESSION_OPTIONS | {
        "timeout_sec",
        "strip_provider_env",
    }

    @property
    def name(self) -> str:
        return "claudep"

    def start_session(self, mcp_servers: list[MCPServer], **kwargs: Any) -> ClaudePSession:
        self.warn_unknown_options(kwargs)
        return ClaudePSession(
            mcp_servers=mcp_servers,
            cwd=self.resolve_cwd(kwargs.get("cwd")),
            model=kwargs.get("model"),
            system_prompt=kwargs.get("system_prompt"),
            session_id=kwargs.get("session_id"),
            disallowed_tools=kwargs.get("disallowed_tools"),
            reasoning=kwargs.get("reasoning"),
            timeout_sec=kwargs.get("timeout_sec", _DEFAULT_TIMEOUT_SEC),
            strip_provider_env=kwargs.get("strip_provider_env", True),
        )

    def resume_session(
        self,
        mcp_servers: list[MCPServer],
        *,
        session_id: str,
        resume_key: str,
        trajectory: Trajectory | None = None,
        **kwargs: Any,
    ) -> ClaudePSession:
        self.warn_unknown_options(kwargs)
        session = ClaudePSession(
            mcp_servers=mcp_servers,
            cwd=self.resolve_cwd(kwargs.get("cwd")),
            model=kwargs.get("model") or (trajectory.model if trajectory else None),
            system_prompt=(trajectory.system_prompt if trajectory else None) or None,
            session_id=resume_key,
            disallowed_tools=kwargs.get("disallowed_tools"),
            reasoning=(trajectory.reasoning if trajectory else None) or None,
            timeout_sec=kwargs.get("timeout_sec", _DEFAULT_TIMEOUT_SEC),
            strip_provider_env=kwargs.get("strip_provider_env", True),
        )
        session._has_sent = True
        if trajectory is not None:
            session._restore_from_trajectory(trajectory)
        return session
