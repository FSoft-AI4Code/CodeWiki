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
import logging
import os
import shutil
from typing import Any, Dict, List

from caw import Agent as CawAgent
from caw import ToolGroup

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.cluster_modules import format_potential_core_components
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.utils import count_tokens, is_complex_module, set_main_loop
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
    from caw.providers.codex import CodexSession

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
    except Exception as e:  # never break the spawn on a patch hiccup
        logger.warning("claude allowedTools patch skipped: %s", e)
        return cmd


def _patch_claude_allowed_tools() -> None:
    global _CLAUDE_ALLOWED_PATCH_APPLIED
    if _CLAUDE_ALLOWED_PATCH_APPLIED:
        return
    import subprocess

    from caw.providers import claude_code as _caw_claude

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


class CawBackend(LLMBackend):
    """Routes LLM operations through the claude / codex CLI subscription."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._caw_provider = _resolve_caw_provider(config.provider)
        # main_model is passed straight through; empty string → caw default.
        self._model: str | None = config.main_model or None
        # Resolve once, before any agent-run chdir can move the cwd —
        # os.path.abspath is cwd-relative and _run_module_agent_sync recurses
        # (via generate_sub_module_documentation) while the cwd is pinned.
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
    ) -> str:
        # Blocks the calling thread for the lifetime of the claude/codex
        # subprocess.  Callers running this from an async context (e.g. the
        # documentation_generator) accept this — there is no concurrent work
        # to do while clustering is in flight anyway.
        effective_model = model or self._model
        agent = CawAgent(
            provider=self._caw_provider,
            model=effective_model,
            tools=ToolGroup.READER,
        )
        traj = agent.completion(prompt)
        return traj.result

    # ------------------------------------------------------------------
    # Per-module agent loop
    # ------------------------------------------------------------------

    async def run_module_agent(
        self,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
    ) -> Dict[str, Any]:
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
        return await asyncio.to_thread(
            self._run_module_agent_sync,
            module_name,
            components,
            core_component_ids,
            module_path,
            working_dir,
        )

    def _run_module_agent_sync(
        self,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
        start_depth: int = 1,
        module_tree: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        # ``start_depth`` lets the recursion preserve max_depth across nested
        # _run_module_agent_sync calls — each fresh deps object would otherwise
        # reset current_depth to 1 and silently bypass max_depth guards.
        # ``module_tree`` carries the parent's in-memory tree across the
        # recursion.  Reloading from disk only works at the top level — by the
        # time a sub-agent runs, the parent has staged new branches in memory
        # but has not yet saved (save happens after agent.completion returns).
        from codewiki.src.be.caw_toolkit import CawToolKit  # local import to avoid cycles

        config = self._config
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        if module_tree is None:
            module_tree = file_manager.load_json(module_tree_path)

        overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
        if os.path.exists(overview_docs_path):
            logger.info("✓ Overview docs already exists at %s", overview_docs_path)
            return module_tree
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
        _, components_with_code = format_potential_core_components(
            core_component_ids, components
        )
        num_tokens = count_tokens(components_with_code)
        can_delegate = (
            is_complex_module(components, core_component_ids)
            and start_depth < config.max_depth
            and num_tokens >= config.max_token_per_leaf_module
        )
        logger.info(f"Module {module_name} can delegate: {can_delegate} - is_complex_module: {is_complex_module(components, core_component_ids)} - start_depth: {start_depth} - num_tokens: {num_tokens} - max_depth: {config.max_depth} - max_token_per_leaf_module: {config.max_token_per_leaf_module}")

        if can_delegate:
            system_prompt = format_system_prompt(module_name, custom_instructions)
        else:
            system_prompt = format_leaf_system_prompt(module_name, custom_instructions)

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

        user_prompt = format_user_prompt(
            module_name=module_name,
            core_component_ids=core_component_ids,
            components=components,
            module_tree=deps.module_tree,
        )

        # caw forks claude / codex via subprocess.Popen without a cwd, so the
        # child CLI inherits Python's cwd — typically the repo root where the
        # user invoked ``codewiki``.  Codex's native ``file_change`` tool
        # (always present under the danger-full-access sandbox EXEC requires)
        # then resolves relative paths against that cwd, dropping the .md at
        # the repo root.  Pin cwd to the docs output dir for the duration of
        # the agent run so file_change lands inside ``--output``.  Reads still
        # go through MCP tools that use absolute paths from ``deps``, so
        # they're cwd-independent.  Safe to mutate process-wide cwd because
        # documentation_generator processes modules sequentially and recursive
        # _run_module_agent_sync calls chdir to the same absolute_docs_path.
        # caw runs claude with `--dangerously-skip-permissions`, expecting
        # bypassPermissions ("Everything" runs without asking).  But an org
        # managed policy can DISABLE bypass mode, in which case the flag is
        # downgraded to `acceptEdits` — observed here: both
        # `--dangerously-skip-permissions` and `--permission-mode
        # bypassPermissions` report permissionMode=acceptEdits.
        #   permission modes: https://code.claude.com/docs/en/permission-modes
        #   (see #skip-all-checks-with-bypasspermissions-mode and its
        #    disableBypassPermissionsMode managed setting).
        # Under acceptEdits, auto-approval is limited to reads/edits INSIDE the
        # working directory or additionalDirectories; paths outside that scope
        # still prompt (== denied in non-interactive `-p`):
        #   https://code.claude.com/docs/en/permission-modes#auto-approve-file-edits-with-acceptedits-mode
        # caw chdir's into the output subdir (for codex's native file_change),
        # so the source tree in the PARENT repo is out-of-scope and every
        # source Read is denied -> empty docs.  claude writes via CodeWiki's
        # str_replace_editor (absolute deps path), so it does NOT need cwd
        # pinned to the output dir.  Pin cwd to the repo root so BOTH the source
        # tree and the output dir fall inside the workspace (alt: --add-dir,
        # https://code.claude.com/docs/en/cli-reference).  Codex keeps the
        # output-dir chdir because its file_change resolves relative paths.
        original_cwd = os.getcwd()
        if self._caw_provider == "codex":
            run_cwd = working_dir
        else:
            run_cwd = self._repo_root
        try:
            os.chdir(run_cwd)
            try:
                traj = agent.completion(user_prompt)
            finally:
                os.chdir(original_cwd)
            logger.info(
                "Module %s completed via caw (turns=%d, tool_calls=%d)",
                module_name,
                traj.num_turns,
                traj.total_tool_calls,
            )
            file_manager.save_json(deps.module_tree, module_tree_path)
            return deps.module_tree
        except Exception as e:
            logger.error("Error processing module %s via caw: %s", module_name, e)
            raise
