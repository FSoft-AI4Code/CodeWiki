"""CawToolKit — CodeWiki's three agent tools exposed as a caw MCP server.

A new toolkit instance is created per agent session (top-level module *or*
sub-module).  State lives on ``self``:

* ``self._deps`` is the live :class:`CodeWikiDeps` for the current module
* ``self._backend`` is a back-reference to :class:`CawBackend` so the
  sub-module recursion can start a fresh session without importing
  :mod:`codewiki.src.be.caw_backend` at module load time (circular import)

Claude Code's built-in writers (``Write``/``Edit``/``NotebookEdit``) and
``Bash`` are disabled at the agent level (see :func:`CawBackend._run_module_agent_sync`).
The agent must use ``str_replace_editor`` for all file writes so Mermaid
validation runs uniformly across both backends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from caw import ToolKit, tool
from mcp.server.fastmcp import Context

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.module_naming import normalize_sub_module_specs

if TYPE_CHECKING:
    from codewiki.src.be.caw_backend import CawBackend

logger = logging.getLogger(__name__)


_HEARTBEAT_INTERVAL_SEC = 10

_VALID_WORKING_DIRS = ("repo", "docs")
_VALID_EDITOR_COMMANDS = ("view", "create", "str_replace", "insert", "undo_edit")


async def _heartbeat(ctx: Context, work: asyncio.Task) -> None:
    # Keeps the codex / claude-code CLI from cancelling the parent tool call
    # during long sub-module recursion. Failures are swallowed — a broken
    # heartbeat must never abort real work.
    progress = 0
    while not work.done():
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
        progress += 1
        try:
            await ctx.report_progress(
                progress=progress,
                total=None,
                message="sub-module generation in progress",
            )
        except Exception:
            pass


def _coerce_json_arg(value):
    # Some MCP/CLI bridges emit list/int tool args as JSON-encoded strings
    # (e.g. ``"[1, 50]"`` instead of ``[1, 50]``).  Parity with the
    # pydantic-ai tool path; see _coerce_json_string in str_replace_editor.py.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


class CawToolKit(
    ToolKit,
    server_name="codewiki_tools",
    display_name="CodeWiki Tools",
):
    """MCP tool server exposing CodeWiki tools to a caw Agent."""

    def __init__(
        self,
        deps: CodeWikiDeps,
        backend: "CawBackend",
        allow_subagent: bool,
    ) -> None:
        self._deps = deps
        self._backend = backend
        self._allow_subagent = allow_subagent

    # ------------------------------------------------------------------
    # Tool: read_code_components
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Read the source code of the given component ids. "
            "component_ids is a list of strings like "
            "['sweagent/types.py::AgentRunResult', 'auth/middleware.py::verify_token'] "
            "where the part before '::' is the file path and the part after is the component name."
        )
    )
    async def read_code_components(self, component_ids: list[str]) -> str:
        results = []
        for cid in component_ids:
            if cid not in self._deps.components:
                results.append(f"# Component {cid} not found")
            else:
                results.append(
                    f"# Component {cid}:\n"
                    f"{self._deps.components[cid].source_code.strip()}\n\n"
                )
        return "\n".join(results)

    # ------------------------------------------------------------------
    # Tool: str_replace_editor
    # Reuses the EditTool implementation + Mermaid validator from the
    # existing module so behavior matches the pydantic-ai path exactly.
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Custom editing tool for viewing, creating and editing files.\n"
            "* If `path` is a file, `view` displays the result of applying `cat -n`. "
            "If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep.\n"
            "* The `create` command cannot be used if the specified `path` already exists as a file.\n"
            "* If a `command` generates a long output, it will be truncated and marked with `<response clipped>`.\n"
            "* The `undo_edit` command will revert the last edit made to the file at `path`.\n"
            "* Only `view` command is allowed when `working_dir` is `repo`."
        )
    )
    async def str_replace_editor(
        self,
        working_dir: str,
        command: str,
        path: str | None = None,
        file: str | None = None,
        file_text: str | None = None,
        view_range: list[int] | str | None = None,
        old_str: str | None = None,
        new_str: str | None = None,
        insert_line: int | str | None = None,
    ) -> str:
        from codewiki.src.be.agent_tools.str_replace_editor import EditTool
        from codewiki.src.be.utils import validate_mermaid_diagrams

        # ``Literal`` annotations would be the cleanest way to constrain these,
        # but ``from __future__ import annotations`` turns them into forward refs
        # that FastMCP's pydantic schema rebuild cannot resolve.  Validate at
        # call time instead so a bogus working_dir (empty string, ``"."``, etc.)
        # cannot silently route writes to the repo root.
        if working_dir not in _VALID_WORKING_DIRS:
            return (
                f"Error: invalid `working_dir`={working_dir!r}. "
                f"Allowed values: {list(_VALID_WORKING_DIRS)}."
            )
        if command not in _VALID_EDITOR_COMMANDS:
            return (
                f"Error: invalid `command`={command!r}. "
                f"Allowed values: {list(_VALID_EDITOR_COMMANDS)}."
            )

        if path is None and file is None:
            return "Error: Either `path` or `file` parameter must be provided."
        if path is None:
            path = file
        if command != "view" and working_dir == "repo":
            return "The `view` command is the only allowed command when `working_dir` is `repo`."

        # Reject absolute paths: ``Path("/abs/base") / "/abs/other"`` resolves to
        # ``/abs/other``, which would silently bypass ``working_dir`` and let the
        # agent write outside the docs path.  Force the agent to pass a path
        # relative to the chosen working_dir.
        if os.path.isabs(path):
            return (
                f"Error: `path` must be relative to `working_dir` ({working_dir!r}), "
                f"got absolute path {path!r}. Pass a relative path like "
                f"'module_name.md' (resolved under absolute_docs_path when "
                f"working_dir='docs')."
            )

        view_range = _coerce_json_arg(view_range)
        insert_line = _coerce_json_arg(insert_line)

        edit_tool = EditTool(self._deps.registry, self._deps.absolute_docs_path)

        base_dir = (
            self._deps.absolute_docs_path
            if working_dir == "docs"
            else self._deps.absolute_repo_path
        )
        absolute_path = str(Path(base_dir) / path)

        # Defense in depth: even with a relative path, ``..`` segments could
        # escape ``base_dir``.  Verify the resolved path stays inside.
        try:
            resolved = Path(absolute_path).resolve()
            base_resolved = Path(base_dir).resolve()
            resolved.relative_to(base_resolved)
        except ValueError:
            return (
                f"Error: resolved path {absolute_path!r} escapes "
                f"working_dir={working_dir!r} root {base_dir!r}. "
                f"Pass a path that stays inside the working directory."
            )

        edit_tool(
            command=command,
            path=absolute_path,
            file_text=file_text,
            view_range=view_range,
            old_str=old_str,
            new_str=new_str,
            insert_line=insert_line,
        )

        result = "\n".join(edit_tool.logs)

        if command != "view" and path.endswith(".md"):
            mermaid_validation = await validate_mermaid_diagrams(absolute_path, path)
            result = result + "\n---------- Mermaid validation ----------\n" + mermaid_validation

        return result

    # ------------------------------------------------------------------
    # Tool: generate_sub_module_documentation
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Delegate documentation generation of sub-modules to sub-agents. Each sub-module "
            "is documented separately.\n"
            "sub_module_specs: a dictionary mapping sub-module names to their core component IDs. "
            "Example: {'authentication': ['auth_handler.py::AuthHandler'], "
            "'database': ['db_client.py::DBClient']}\n"
            "Sub-module names must be unique across the whole wiki; a name already used by another "
            "module is automatically prefixed with the current module name, and the tool result "
            "reports the final file names actually saved."
        )
    )
    async def generate_sub_module_documentation(
        self, sub_module_specs: dict[str, list[str]], ctx: Context
    ) -> str:
        if not self._allow_subagent:
            return (
                "generate_sub_module_documentation is NOT available for this module "
                "(leaf module: single-file or below the token threshold, or max recursion "
                "depth reached). DO NOT call this tool again for this module. "
                "Instead, write the documentation directly with `str_replace_editor` "
                f"(create command) as a single `{self._deps.current_module_name}.md` "
                "file covering the provided core components inline (architecture, "
                "components, diagrams, etc.) — no sub-module fan-out."
            )

        # Run the blocking recursion in a worker thread so the caw MCP server's
        # event loop stays responsive while sub-agents run.  A heartbeat task
        # emits MCP progress notifications so the CLI does not treat the long
        # tool call as a stalled / cancelled invocation.
        work = asyncio.create_task(
            asyncio.to_thread(self._run_sub_modules, sub_module_specs)
        )
        heartbeat = asyncio.create_task(_heartbeat(ctx, work))
        try:
            return await work
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    # ------------------------------------------------------------------
    # Internal: synchronous recursion driver
    # ------------------------------------------------------------------

    def _run_sub_modules(self, sub_module_specs: dict[str, list[str]]) -> str:
        deps = self._deps
        previous_module_name = deps.current_module_name

        # Resolve name collisions against the module tree and files already on
        # disk before touching the tree (issue #76): docs live in one flat directory.
        name_map = normalize_sub_module_specs(
            sub_module_specs,
            previous_module_name,
            deps.module_tree,
            deps.absolute_docs_path,
        )
        final_specs = {
            name_map[requested_name]: core_ids
            for requested_name, core_ids in sub_module_specs.items()
        }

        # Add sub-modules to the in-memory module tree.
        value = deps.module_tree
        for key in deps.path_to_current_module:
            value = value[key]["children"]
        for sub_name, core_ids in final_specs.items():
            value[sub_name] = {"components": core_ids, "children": {}}

        try:
            for sub_name, core_ids in final_specs.items():
                indent = "  " * deps.current_depth
                arrow = "└─" if deps.current_depth > 0 else "→"
                logger.info("%s%s Generating documentation for sub-module: %s", indent, arrow, sub_name)

                deps.current_module_name = sub_name
                deps.path_to_current_module.append(sub_name)
                deps.current_depth += 1
                try:
                    # Spawn a fresh caw session for the sub-module.  We already
                    # run inside a worker thread (started by the parent tool
                    # call), so call the sync entry point directly to avoid
                    # double-wrapping.  ``start_depth`` carries the parent's
                    # depth so the sub-agent's max_depth guard stays accurate.
                    self._backend._run_module_agent_sync(
                        module_name=sub_name,
                        components=deps.components,
                        core_component_ids=core_ids,
                        module_path=list(deps.path_to_current_module),
                        working_dir=deps.absolute_docs_path,
                        start_depth=deps.current_depth,
                        module_tree=deps.module_tree,
                    )
                finally:
                    deps.path_to_current_module.pop()
                    deps.current_depth -= 1
        finally:
            deps.current_module_name = previous_module_name

        # Report what actually landed on disk so the parent agent links real filenames.
        saved = []
        missing = []
        for requested_name, final_name in name_map.items():
            entry = f"{final_name}.md"
            if final_name != requested_name:
                entry += f" (requested '{requested_name}', renamed to avoid a collision)"
            if os.path.exists(os.path.join(deps.absolute_docs_path, f"{final_name}.md")):
                saved.append(entry)
            else:
                missing.append(entry)

        report = f"Saved documentations: {', '.join(saved) if saved else 'none'}."
        if missing:
            report += f" MISSING (generation did not produce these files): {', '.join(missing)}."
            logger.warning("Sub-module documentation missing after generation: %s", ", ".join(missing))
        return report
