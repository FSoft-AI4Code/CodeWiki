"""Tool for overview agents to read generated child module documentation."""

import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import RunContext, Tool

logger = logging.getLogger(__name__)

MAX_DOC_READ_CHARS = 160_000


@dataclass
class OverviewDeps:
    """Dependencies for parent / repository overview pydantic-ai agents."""

    absolute_docs_path: str


async def read_module_documentation(ctx: RunContext[OverviewDeps], file_path: str) -> str:
    """Read a generated module documentation markdown file.

    Args:
        file_path: Absolute path from the ``docs_path`` in REPO_STRUCTURE. A
            path relative to the docs directory is also accepted (e.g.
            ``"Foo.md"`` or ``"sub/Foo.md"``), including one that still
            repeats the docs directory's own name as a leading segment
            (e.g. ``"docs/Foo.md"``).
    """
    logger.info(f"Reading module documentation from {file_path}")
    docs_root = Path(ctx.deps.absolute_docs_path).resolve()
    raw = file_path.strip()
    candidate = Path(raw)

    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        candidates = [docs_root / candidate]
        if candidate.parts and candidate.parts[0] == docs_root.name:
            candidates.append(docs_root / Path(*candidate.parts[1:]))
        resolved = next(
            (c.resolve() for c in candidates if c.resolve().is_file()),
            candidates[0].resolve(),
        )

    try:
        resolved.relative_to(docs_root)
    except ValueError:
        return f"Error: {file_path!r} is outside the documentation directory."
    if not resolved.is_file():
        logger.warning(f"Documentation file not found: {resolved}")
        return f"Error: documentation file not found: {resolved}"
    if resolved.suffix.lower() != ".md":
        logger.warning(f"Only .md documentation files can be read (got {resolved.suffix!r}).")
        return f"Error: only .md documentation files can be read (got {resolved.suffix!r})."
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_DOC_READ_CHARS:
        logger.warning(f"Documentation file is too long (got {len(text)} characters).")
        text = text[:MAX_DOC_READ_CHARS] + "\n\n<truncated>"
    return text


read_module_documentation_tool = Tool(
    function=read_module_documentation,
    name="read_module_documentation",
    description="Read a child module's generated markdown documentation. Use the docs_path value from REPO_STRUCTURE.",
    takes_ctx=True,
)
