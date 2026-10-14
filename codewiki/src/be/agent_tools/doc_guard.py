"""Make a documentation agent actually write its page.

A module agent can end its run with a text answer and never call
``str_replace_editor create`` (or call it only with arguments the tool
rejects). The run then "succeeds" but the page is missing, and the CLI only
notices at the very end; re-running repeats the same miss (issue #134).

:func:`run_until_doc_written` checks the page after each run and, when it is
missing, continues the same conversation with a reminder naming the exact
file to create. Every miss is logged with the agent's final answer and a
summary of its tool calls, so a page that is still missing can be diagnosed.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from codewiki.src.be.doc_layout import OVERVIEW_FILENAME, module_doc_file
from codewiki.src.be.module_naming import resolve_module_doc_path

logger = logging.getLogger(__name__)

# Follow-up turns sent to an agent that finished without writing its page.
DOC_WRITE_REMINDERS = 2

_EXCERPT_CHARS = 300


def doc_written(
    docs_dir: str, module_name: str, module_path: list[str], module_tree: dict[str, Any] | None
) -> bool:
    """Whether the page the agent for ``module_name`` must write exists."""
    if not module_path:
        # The whole-repository agent writes {repo_name}.md, renamed to overview.md later.
        return os.path.isfile(os.path.join(docs_dir, f"{module_name}.md")) or os.path.isfile(
            os.path.join(docs_dir, OVERVIEW_FILENAME)
        )
    return resolve_module_doc_path(docs_dir, module_name, module_tree) is not None


def reminder_prompt(module_name: str, doc_file: str) -> str:
    return (
        f"You finished without creating `{doc_file}`, so the documentation for "
        f"`{module_name}` is missing. Create it now: call `str_replace_editor` with "
        f'working_dir="docs", command="create", path="./{doc_file}" and the complete '
        f"Markdown page as file_text. If an earlier tool call returned an error, fix the "
        f"arguments and try again. Do not reply with the documentation as text."
    )


def _excerpt(text: Any) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > _EXCERPT_CHARS:
        return text[:_EXCERPT_CHARS] + "..."
    return text or "<empty>"


def _editor_command(part: ToolCallPart) -> str:
    try:
        args = part.args_as_dict()
    except Exception:  # noqa: BLE001 — malformed args are reported as such
        return "<unparseable args>"
    return f"{args.get('command')} {args.get('path') or args.get('file')}"


def summarize_tool_activity(messages: list[Any]) -> str:
    """One-line summary of an agent run: tool calls, editor writes and their results,
    rejected tool calls and why the last response ended."""
    calls: Counter[str] = Counter()
    writes: dict[str, str] = {}
    results: dict[str, str] = {}
    rejected: list[str] = []
    finish_reason = None
    for message in messages:
        if isinstance(message, ModelResponse):
            finish_reason = getattr(message, "finish_reason", None)
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    calls[part.tool_name] += 1
                    if part.tool_name == "str_replace_editor":
                        command = _editor_command(part)
                        if not command.startswith("view "):
                            writes[part.tool_call_id] = command
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id in writes:
                    lines = str(part.content).strip().splitlines()
                    results[part.tool_call_id] = _excerpt(lines[0] if lines else "")
                elif isinstance(part, RetryPromptPart) and part.tool_name:
                    rejected.append(f"{part.tool_name}: {_excerpt(part.model_response())}")

    summary = [
        "tool calls: "
        + (", ".join(f"{name} x{count}" for name, count in sorted(calls.items())) or "none")
    ]
    if writes:
        summary.append(
            "editor writes: "
            + "; ".join(
                f"{command} -> {results.get(call_id, 'no result')}"
                for call_id, command in writes.items()
            )
        )
    else:
        summary.append("editor writes: none")
    if rejected:
        summary.append(f"rejected tool calls ({len(rejected)}): " + "; ".join(rejected[-3:]))
    summary.append(f"last finish_reason: {finish_reason}")
    return " | ".join(summary)


def run_usage(result: Any) -> Any:
    """``usage`` is a property in pydantic-ai 2.x and a method in earlier releases."""
    usage = getattr(result, "usage", None)
    return usage() if callable(usage) else usage


async def run_until_doc_written(
    agent: Any,
    user_prompt: str,
    *,
    deps: Any,
    module_name: str,
    module_path: list[str],
    layout: str | None,
    usage_limits: Any = None,
) -> tuple[Any, Any]:
    """Run ``agent`` and remind it (up to :data:`DOC_WRITE_REMINDERS` times) to
    write its page if it ended without one.

    Returns the last run's result and the token usage summed over all runs.
    Each reminder is a separate run with its own ``usage_limits``, so a first
    run that used most of its request budget still gets a chance to write.
    """
    doc_file = module_doc_file(module_name, module_path, layout)
    result = await agent.run(user_prompt, deps=deps, usage_limits=usage_limits)
    usage = run_usage(result)
    for attempt in range(1, DOC_WRITE_REMINDERS + 1):
        if doc_written(deps.absolute_docs_path, module_name, module_path, deps.module_tree):
            return result, usage
        logger.warning(
            "Agent for %s finished without writing %s (reminder %d/%d). Final answer: %s | %s",
            module_name,
            doc_file,
            attempt,
            DOC_WRITE_REMINDERS,
            _excerpt(result.output),
            summarize_tool_activity(result.new_messages()),
        )
        result = await agent.run(
            reminder_prompt(module_name, doc_file),
            deps=deps,
            usage_limits=usage_limits,
            message_history=result.all_messages(),
        )
        more = run_usage(result)
        usage = more if usage is None else usage if more is None else usage + more
    if not doc_written(deps.absolute_docs_path, module_name, module_path, deps.module_tree):
        logger.error(
            "Agent for %s still has not written %s after %d reminders. Final answer: %s | %s",
            module_name,
            doc_file,
            DOC_WRITE_REMINDERS,
            _excerpt(result.output),
            summarize_tool_activity(result.new_messages()),
        )
    return result, usage
