"""CawBackend — subscription-mode backend using the ``claude`` / ``codex`` CLIs.

Implements :class:`LLMBackend` by routing all completions and agent runs
through :mod:`caw`, which wraps the official Claude Code and Codex CLI
binaries.  Authentication is the user's existing OAuth subscription — no
API key is needed.

Provider mapping:

* ``provider="claude-code"`` → caw provider ``"claude_code"``
* ``provider="codex"``       → caw provider ``"codex"``

``config.main_model`` is passed straight through to caw.  caw forwards it
to ``claude --model`` / ``codex --model``; whichever values those CLIs
accept are valid here.  ``config.cluster_model`` is honored per-call when
passed explicitly through :meth:`complete`.  ``config.fallback_model`` is
ignored (caw has no built-in fallback chain).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import tempfile
import time
import shutil
from typing import Any

from codewiki._vendor.caw import Agent as CawAgent
from codewiki._vendor.caw import ToolGroup

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.backend import AgentReply, LLMBackend
from codewiki.src.be.cluster_modules import format_potential_core_components
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.utils import count_tokens, is_complex_module, set_main_loop
from codewiki.src.be.doc_layout import config_layout, find_doc, module_doc_file
from codewiki.src.config import MODULE_TREE_FILENAME, OVERVIEW_FILENAME, Config
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)


_CAW_PROVIDER_MAP = {
    "claude-code": "claude_code",
    "codex": "codex",
}

_CLI_BINARY = {
    "claude-code": "claude",
    "codex": "codex",
}

# Disable WRITER (Write/Edit/NotebookEdit) so the agent must use CodeWiki's
# str_replace_editor and Mermaid validation runs.  INTERACTION (AskUserQuestion)
# and WEB (WebFetch/WebSearch) are also off.  PARALLEL (Task) stays enabled:
# Claude Code can fan out Read-heavy exploration without affecting
# documentation correctness.
_AGENT_TOOL_GROUP = ToolGroup.READER | ToolGroup.PARALLEL


def _agent_tool_group_for_provider(provider: str) -> ToolGroup:
    """Return the caw tool group needed for a module-agent session."""
    if provider == "codex":
        # Codex CLI 0.118+ discovers streamable-HTTP MCP servers in read-only
        # or workspace-write sandbox mode, but cancels MCP tool calls in
        # non-interactive `codex exec` with "user cancelled MCP tool call".
        # In caw's Codex adapter, including EXEC maps the session to
        # `--dangerously-bypass-approvals-and-sandbox`, which is currently the
        # mode where CodeWiki's MCP tools run reliably.  Codex cannot enforce
        # finer-grained built-in tool restrictions anyway, so prompts still
        # direct the agent to use CodeWiki's str_replace_editor for writes.
        return _AGENT_TOOL_GROUP | ToolGroup.EXEC
    return _AGENT_TOOL_GROUP


def _resolve_caw_provider(provider: str) -> str:
    try:
        return _CAW_PROVIDER_MAP[provider]
    except KeyError as e:
        raise ValueError(
            f"Unsupported caw provider {provider!r}. Expected one of: "
            f"{sorted(_CAW_PROVIDER_MAP.keys())}"
        ) from e


# --- caw codex tool_timeout_sec stopgap ---------------------------------------
# Upstream caw's CodexSession._mcp_config_args (caw/providers/codex.py) emits
# no per-server tool_timeout_sec flag, so codex cancels long sub-module
# recursion. Remove this block once upstream lands a typed knob.
_CODEX_PATCH_APPLIED = False
_CODEX_TOOL_TIMEOUT_SEC = 86400  # 24 h


def _patch_codex_tool_timeout() -> None:
    global _CODEX_PATCH_APPLIED
    if _CODEX_PATCH_APPLIED:
        return
    from codewiki._vendor.caw.providers.codex import CodexSession

    _orig = CodexSession._mcp_config_args

    def _patched(self) -> list[str]:
        args = list(_orig(self))
        for srv in self._mcp_servers:
            args += [
                "-c",
                f"mcp_servers.{srv.name}.tool_timeout_sec={_CODEX_TOOL_TIMEOUT_SEC}",
            ]
        return args

    CodexSession._mcp_config_args = _patched
    _CODEX_PATCH_APPLIED = True


_patch_codex_tool_timeout()
# --- end stopgap --------------------------------------------------------------


# --- claude-code allowedTools stopgap -----------------------------------------
# Custom MCP-server tools (added via `--mcp-config`) are not auto-approved under
# `acceptEdits`, and `bypassPermissions` may be disabled by an org managed
# policy (see the cwd note below), so caw's `--dangerously-skip-permissions`
# alone is not enough: CodeWiki's own toolkit (str_replace_editor,
# read_code_components, generate_sub_module_documentation) gets denied
# ("you haven't granted it yet"), the agent writes nothing, and the run
# "succeeds" with an empty module tree.  Grant the toolkit explicitly with
# `--allowedTools` using the permission rule syntax:
#   https://code.claude.com/docs/en/settings#permission-rule-syntax
#   `--allowedTools` flag: https://code.claude.com/docs/en/cli-reference
# caw's ClaudeCodeSession only ever emits `--disallowedTools`, so rewrite its
# `claude` command to add `--allowedTools mcp__<server>` for every server in
# the --mcp-config.  The patch swaps the `subprocess` module reference INSIDE
# caw.providers.claude_code for a thin proxy — the global subprocess.Popen
# class stays untouched (isinstance / subclass safe) and no other claude
# invocation in this process is affected.  Belongs upstream in caw; remove
# once it grows a first-class allowed_tools knob for its toolkit servers.
_CLAUDE_ALLOWED_PATCH_APPLIED = False


def _with_allowed_tools(cmd):
    """Append ``--allowedTools mcp__<server>,...`` to a ``claude`` command.

    Pure ``list -> list`` transform.  Applies only when *cmd* is a claude
    invocation carrying ``--mcp-config`` and no explicit allow-list already;
    otherwise (or on any error) *cmd* is returned unchanged.
    """
    import json

    try:
        if not (
            isinstance(cmd, (list, tuple))
            and cmd
            and os.path.basename(str(cmd[0])) == "claude"
            and "--mcp-config" in cmd
            and "--allowedTools" not in cmd
            and "--allowed-tools" not in cmd
        ):
            return cmd
        # caw emits a single `--mcp-config <path>`; only the first occurrence
        # is considered.
        cfg_path = cmd[list(cmd).index("--mcp-config") + 1]
        with open(cfg_path) as f:
            servers = list(json.load(f).get("mcpServers", {}).keys())
        if not servers:
            return cmd
        allowed = ",".join(f"mcp__{s}" for s in servers)
        logger.info("Injected --allowedTools for MCP servers: %s", servers)
        return list(cmd) + ["--allowedTools", allowed]
    except Exception as e:  # noqa: BLE001 — never break the spawn on a patch hiccup
        logger.warning("claude allowedTools patch skipped: %s", e)
        return cmd


def _patch_claude_allowed_tools() -> None:
    global _CLAUDE_ALLOWED_PATCH_APPLIED
    if _CLAUDE_ALLOWED_PATCH_APPLIED:
        return
    import subprocess

    from codewiki._vendor.caw.providers import claude_code as _caw_claude

    class _SubprocessProxy:
        """``subprocess`` stand-in that rewrites Popen commands.

        Every other attribute (PIPE, run, ...) delegates to the real module.
        """

        @staticmethod
        def Popen(cmd, *args, **kwargs):
            return subprocess.Popen(_with_allowed_tools(cmd), *args, **kwargs)

        def __getattr__(self, name):
            return getattr(subprocess, name)

    _caw_claude.subprocess = _SubprocessProxy()
    _CLAUDE_ALLOWED_PATCH_APPLIED = True


_patch_claude_allowed_tools()
# --- end stopgap --------------------------------------------------------------


# Below this a final reply is a status note ("Done."), not a page.
_MIN_FALLBACK_DOC_CHARS = 500


def save_unwritten_doc(docs_path: str, final_text: str | None, module_name: str) -> bool:
    """Save the agent's final reply as the module page if it never wrote one.

    Subscription CLIs sometimes answer with the whole page as their final
    message instead of calling str_replace_editor, so the run "completes"
    with no file and generation then fails on the missing doc. Only a reply
    that looks like a page (a ``#`` heading, substantial length) is saved;
    anything else is left for the missing-docs check to report.
    """
    if os.path.exists(docs_path) or not final_text:
        return False
    lines = final_text.strip().splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if start is None:
        return False
    content = "\n".join(lines[start:]).strip()
    if len(content) < _MIN_FALLBACK_DOC_CHARS:
        return False
    logger.warning(
        "Module %s finished without writing %s; saving its final reply as the page.",
        module_name,
        docs_path,
    )
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    file_manager.save_text(content + "\n", docs_path)
    return True


def neutral_cwd() -> str:
    """An empty working directory for prompt-only CLI calls.

    The CLIs load project context from their working directory (claude:
    CLAUDE.md and the project's auto-memory). A prompt-only call must see
    just its prompt, not whatever project the user's shell is in.
    """
    path = os.path.join(tempfile.gettempdir(), "codewiki-plain-completion")
    os.makedirs(path, exist_ok=True)
    return path


# A prompt-only answer takes well under a minute; a CLI that is still running
# after this is stuck (e.g. a wrapper script waiting on something).
_PLAIN_TIMEOUT_SEC = 300


class CawBackend(LLMBackend):
    """Routes LLM operations through the claude / codex CLI subscription."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._caw_provider = _resolve_caw_provider(config.provider)
        # main_model is passed straight through; empty string → caw default.
        self._model: str | None = config.main_model or None
        # Resolve once: os.path.abspath is cwd-relative.
        self._repo_root = str(os.path.abspath(config.repo_path))

        # Fail loudly here rather than producing a confusing caw error mid-run.
        cli = _CLI_BINARY[config.provider]
        if shutil.which(cli) is None:
            raise RuntimeError(
                f"Subscription mode requires the '{cli}' CLI on PATH. "
                f"Install it and run '{cli} login', then try again."
            )

        if self._caw_provider == "claude_code":
            # Prevent claude-code CLI from cancelling long sub-module recursion;
            # setdefault preserves a user-supplied value (e.g. shell override).
            os.environ.setdefault("MCP_TOOL_TIMEOUT", "86400000")
            os.environ.setdefault("MCP_TIMEOUT", "60000")
            logger.info(
                "claude-code MCP timeouts: MCP_TOOL_TIMEOUT=%s MCP_TIMEOUT=%s",
                os.environ["MCP_TOOL_TIMEOUT"],
                os.environ["MCP_TIMEOUT"],
            )

    # ------------------------------------------------------------------
    # Single-shot completion (clustering, parent / repo overviews)
    # ------------------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        allow_tools: bool = True,
    ) -> str:
        # Blocks the calling thread for the lifetime of the claude/codex
        # subprocess.  Callers running this from an async context (e.g. the
        # documentation_generator) accept this — there is no concurrent work
        # to do while clustering is in flight anyway.
        effective_model = model or self._model
        if not allow_tools and self._caw_provider == "claude_code":
            return self._plain_claude_completion(prompt, system_prompt, effective_model)
        agent = CawAgent(
            provider=self._caw_provider,
            model=effective_model,
            # caw rejects an empty tool set; INTERACTION alone (AskUserQuestion,
            # unusable in -p) leaves no file, shell or web access.
            tools=ToolGroup.READER if allow_tools else ToolGroup.INTERACTION,
            system_prompt=system_prompt,
        )
        started = time.time()
        if allow_tools:
            traj = agent.completion(prompt)
        else:
            traj = agent.completion(prompt, cwd=neutral_cwd())
        self._record_usage(
            "complete",
            "",
            getattr(traj, "total_usage", None),
            seconds=time.time() - started,
            model=getattr(traj, "model", None) or effective_model,
        )
        return traj.result

    def _plain_claude_completion(
        self, prompt: str, system_prompt: str | None, model: str | None
    ) -> str:
        """``claude -p`` with no tools, no MCP servers and a neutral cwd.

        caw cannot pass these flags. Without them every call also carries
        Claude Code's tool definitions and the user's MCP servers: ~17k
        prompt tokens of overhead against ~500 (measured), plus the
        project memory of whatever directory the shell is in.
        """
        started = time.time()
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
        ]
        if model:
            cmd += ["--model", model]
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, prompt on stdin
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=neutral_cwd(),
                timeout=_PLAIN_TIMEOUT_SEC,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"claude did not answer within {_PLAIN_TIMEOUT_SEC}s. Check that "
                "`claude -p hello` works from this process's environment (an MCP client "
                "may start CodeWiki with a reduced PATH/environment)."
            ) from e
        data = None
        for line in reversed(proc.stdout.splitlines()):  # shims may print before the JSON
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        if data is None:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise RuntimeError(f"claude returned no result (exit {proc.returncode}): {tail}")
        usage = data.get("usage") or {}
        self._record_usage(
            "complete",
            "",
            {
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
                "cache_write_tokens": usage.get("cache_creation_input_tokens") or 0,
                "cost_usd": data.get("total_cost_usd") or 0.0,
            },
            seconds=time.time() - started,
            model=model,
        )
        result = data.get("result") or ""
        if data.get("is_error"):
            raise RuntimeError(f"claude: {result or data.get('subtype') or 'error'}")
        return result

    # ------------------------------------------------------------------
    # Update agent (incremental updater): read tools + str_replace_editor,
    # no delegation, final message returned.
    # ------------------------------------------------------------------

    async def run_update_agent(
        self,
        system_prompt: str,
        user_prompt: str,
        deps: CodeWikiDeps,
    ) -> AgentReply:
        set_main_loop(asyncio.get_running_loop())
        return await asyncio.to_thread(
            self._run_update_agent_sync, system_prompt, user_prompt, deps
        )

    def _run_update_agent_sync(
        self, system_prompt: str, user_prompt: str, deps: CodeWikiDeps
    ) -> AgentReply:
        from codewiki.src.be.caw_toolkit import CawToolKit  # local import to avoid cycles

        toolkit = CawToolKit(deps=deps, backend=self, allow_subagent=False)
        agent = CawAgent(
            provider=self._caw_provider,
            model=self._model,
            system_prompt=system_prompt,
            tools=_agent_tool_group_for_provider(self._caw_provider),
            tool_servers=[toolkit],
        )
        run_cwd = deps.absolute_docs_path if self._caw_provider == "codex" else self._repo_root
        started = time.time()
        try:
            traj = agent.completion(user_prompt, cwd=run_cwd)
        except Exception as e:
            logger.error("Update agent for %s failed via caw: %s", deps.current_module_name, e)
            raise
        seconds = time.time() - started
        usage = self._record_usage(
            "update_agent",
            deps.current_module_name,
            getattr(traj, "total_usage", None),
            seconds=seconds,
            model=getattr(traj, "model", None) or self._model,
        )
        return AgentReply(
            text=traj.result or "",
            usage=usage,
            seconds=seconds,
            meta={"turns": traj.num_turns, "tool_calls": traj.total_tool_calls},
        )

    # ------------------------------------------------------------------
    # Per-module agent loop
    # ------------------------------------------------------------------

    async def run_module_agent(
        self,
        module_name: str,
        components: dict[str, Node],
        core_component_ids: list[str],
        module_path: list[str],
        working_dir: str,
    ) -> dict[str, Any]:
        # caw.completion shells out to a subprocess and blocks the calling
        # thread.  Push it off the event loop so the rest of the async
        # pipeline keeps moving.
        # Mermaid validation goes through PythonMonkey, which binds its JS
        # engine to the thread where it was first imported (the main
        # thread).  caw routes MCP tool calls through a FastMCP daemon
        # thread, so the validator would otherwise lose its event loop.
        # Hand the main loop to utils so the worker-thread tool calls can
        # marshal parse_mermaid_py back here.
        set_main_loop(asyncio.get_running_loop())
        async with self.slots.hold_async():
            return await asyncio.to_thread(
                self._run_module_agent_sync,
                module_name,
                components,
                core_component_ids,
                module_path,
                working_dir,
                hold_slot=False,
            )

    def _run_module_agent_sync(
        self,
        module_name: str,
        components: dict[str, Node],
        core_component_ids: list[str],
        module_path: list[str],
        working_dir: str,
        start_depth: int = 1,
        module_tree: dict[str, Any] | None = None,
        hold_slot: bool = True,
    ) -> dict[str, Any]:
        # ``start_depth`` lets the recursion preserve max_depth across nested
        # _run_module_agent_sync calls — each fresh deps object would otherwise
        # reset current_depth to 1 and silently bypass max_depth guards.
        # ``module_tree`` carries the parent's in-memory tree across the
        # recursion.  Reloading from disk only works at the top level — by the
        # time a sub-agent runs, the parent has staged new branches in memory
        # but has not yet saved (save happens after agent.completion returns).
        # ``hold_slot`` is False when the caller already holds a concurrency
        # slot for this session (run_module_agent).
        from codewiki.src.be.caw_toolkit import CawToolKit  # local import to avoid cycles

        config = self._config
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        shared = self.module_tree
        if module_tree is None:
            module_tree = shared.get(module_tree_path)

        # overview.md is the root module's own doc (renamed from
        # {repo_name}.md), so its presence only proves the root is done —
        # nested modules must still be checked against their own doc file,
        # or a resume after a partial run silently skips every missing one.
        if not module_path:
            overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
            if os.path.exists(overview_docs_path):
                logger.info("✓ Overview docs already exists at %s", overview_docs_path)
                return module_tree
        with shared.lock:
            docs_path = find_doc(working_dir, module_name, module_tree) if module_path else None
        if docs_path is None:
            docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path):
            logger.info("✓ Module docs already exists at %s", docs_path)
            return module_tree

        custom_instructions = config.get_prompt_addition()

        # Mirror PydanticAIBackend's early-cut: a module is only worth
        # delegating to sub-agents when it spans multiple files AND has enough
        # content to justify the cost AND we still have recursion budget.
        # Without this gate the caw path would give every multi-file sub-module
        # the recursive SYSTEM_PROMPT + delegation tool and fan out one extra
        # agent call per sub-spec even when a single leaf write would suffice.
        # See generate_sub_module_documentation_tool for the pydantic-ai
        # equivalent.
        _, components_with_code = format_potential_core_components(core_component_ids, components)
        num_tokens = count_tokens(components_with_code)
        can_delegate = (
            is_complex_module(components, core_component_ids)
            and start_depth < config.max_depth
            and num_tokens >= config.max_token_per_leaf_module
        )
        logger.info(
            f"Module {module_name} can delegate: {can_delegate} - is_complex_module: {is_complex_module(components, core_component_ids)} - start_depth: {start_depth} - num_tokens: {num_tokens} - max_depth: {config.max_depth} - max_token_per_leaf_module: {config.max_token_per_leaf_module}"
        )

        if can_delegate:
            system_prompt = format_system_prompt(
                module_name, custom_instructions, list(module_path), config_layout(config)
            )
        else:
            system_prompt = format_leaf_system_prompt(
                module_name, custom_instructions, list(module_path), config_layout(config)
            )

        deps = CodeWikiDeps(
            absolute_docs_path=working_dir,
            absolute_repo_path=self._repo_root,
            registry={},
            components=components,
            path_to_current_module=list(module_path),
            current_module_name=module_name,
            module_tree=module_tree,
            max_depth=config.max_depth,
            current_depth=start_depth,
            config=config,
            custom_instructions=custom_instructions,
        )

        toolkit = CawToolKit(deps=deps, backend=self, allow_subagent=can_delegate)

        agent = CawAgent(
            provider=self._caw_provider,
            model=self._model,
            system_prompt=system_prompt,
            tools=_agent_tool_group_for_provider(self._caw_provider),
            tool_servers=[toolkit],
        )

        with shared.lock:
            user_prompt = format_user_prompt(
                module_name=module_name,
                core_component_ids=core_component_ids,
                components=components,
                module_tree=deps.module_tree,
                module_path=list(module_path),
                layout=config_layout(config),
            )

        # Each CLI session gets its own cwd (caw passes it to Popen) instead
        # of a process-wide os.chdir, which concurrent sessions would race on.
        # Codex: its native file_change resolves relative paths against cwd,
        # so it runs in the docs dir to land pages inside ``--output``.
        # Claude: an org policy can downgrade --dangerously-skip-permissions
        # to acceptEdits, which only auto-approves reads inside the cwd, so it
        # runs at the repo root to keep the source tree readable
        # (https://code.claude.com/docs/en/permission-modes).
        run_cwd = working_dir if self._caw_provider == "codex" else self._repo_root
        slot = self.slots.hold() if hold_slot else contextlib.nullcontext()
        started = time.time()
        try:
            with slot:
                traj = agent.completion(user_prompt, cwd=run_cwd)
            # Nested sub-module agents are separate CLI processes that record
            # their own usage, so this covers only this module's own run.
            usage = self._record_usage(
                "module_agent",
                module_name,
                getattr(traj, "total_usage", None),
                seconds=time.time() - started,
                model=getattr(traj, "model", None) or self._model,
            )
            logger.info(
                "Module %s completed via caw (turns=%d, tool_calls=%d, cost=$%.4f)",
                module_name,
                traj.num_turns,
                traj.total_tool_calls,
                (usage or {}).get("cost_usd", 0.0),
            )
            save_unwritten_doc(
                os.path.join(
                    working_dir,
                    module_doc_file(module_name, list(module_path), config_layout(config)),
                ),
                getattr(traj, "result", ""),
                module_name,
            )
            shared.save(module_tree_path, deps.module_tree)
            return deps.module_tree
        except Exception as e:
            logger.error("Error processing module %s via caw: %s", module_name, e)
            raise
