"""MCP tools for answering questions from generated docs + source code.

The retrieval tools (status, outline, search, read, impact) make no LLM
call: they hand the IDE's own model precise, budgeted context. ``ask`` runs
CodeWiki's own pipeline and returns a cited answer.

Results are compact plain text, not indented JSON: every token of a tool
result is a token the IDE model pays to read.
"""

from __future__ import annotations

from typing import Any

from codewiki.src.be.qa import get_index
from codewiki.src.be.qa.retrieve import Retriever

UPDATE_HINT = "Run `codewiki generate --update` in the repository to refresh the stale pages."
DEFAULT_READ_TOKENS = 4000
MAX_READ_TOKENS = 20000


def _kinds(value: str | None) -> set[str] | None:
    if value in (None, "", "all"):
        return None
    if value in ("docs", "doc", "section", "sections"):
        return {"section"}
    if value in ("code", "components"):
        return {"code"}
    raise ValueError("kinds must be one of: all, docs, code")


def _freshness_line(index) -> str:
    line = index.freshness.summary()
    if index.stale_pages:
        line += f" · {len(index.stale_pages)} stale page(s)"
    return line


def handle_status(args: dict[str, Any]) -> str:
    index = get_index(args.get("docs_dir"))
    f = index.freshness
    lines = [
        f"Docs: {index.docs_dir}",
        f"Repository: {index.repo_path or 'not found (code search limited to the snapshot)'}",
        f"Freshness: {_freshness_line(index)} (checked via {f.method})",
        f"Indexed: {len(index.sections)} doc sections, {len(index.code)} code components",
    ]
    if f.touched:
        shown = sorted(f.touched)
        lines.append("Changed files: " + ", ".join(shown[:30]) + (" …" if len(shown) > 30 else ""))
    if index.stale_pages:
        lines.append("Stale pages: " + ", ".join(sorted(index.stale_pages)))
        lines.append(UPDATE_HINT)
    return "\n".join(lines)


def handle_outline(args: dict[str, Any]) -> str:
    index = get_index(args.get("docs_dir"))
    return f"{index.outline}\n\n[{_freshness_line(index)}]"


def handle_search(args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    index = get_index(args.get("docs_dir"))
    k = max(1, min(int(args.get("k") or 10), 50))
    retriever = Retriever(index)
    cands = retriever.search(query, k=k, kinds=_kinds(args.get("kinds")))
    if not cands:
        return f"No matches for {query!r}. Try other words, or codewiki_outline to browse."
    lines = [retriever.line(c) for c in cands]
    lines.append("")
    lines.append(
        "Read what you need with codewiki_read(ids=[...]); ~tok is the cost of reading each. "
        f"[{_freshness_line(index)}]"
    )
    return "\n".join(lines)


def handle_read(args: dict[str, Any]) -> str:
    ids = args.get("ids") or []
    if isinstance(ids, str):
        ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not ids:
        raise ValueError("ids is required")
    index = get_index(args.get("docs_dir"))
    budget = max(200, min(int(args.get("max_tokens") or DEFAULT_READ_TOKENS), MAX_READ_TOKENS))
    retriever = Retriever(index)
    items = retriever.assemble(list(ids), budget)
    read_ids = {item.id for item in items}
    blocks = []
    for item in items:
        notes = []
        if item.stale:
            notes.append("stale doc: the code it describes changed since; prefer the code")
        if item.truncated:
            notes.append("truncated to fit max_tokens")
        note = f" ({'; '.join(notes)})" if notes else ""
        fence = "" if item.kind == "section" else "```"
        body = f"{fence}\n{item.text}\n{fence}" if fence else item.text
        blocks.append(f"### {item.id} · {item.location}{note}\n{body}")
    missing = [i for i in ids if i not in index.sections and i not in index.code]
    skipped = [i for i in ids if i not in read_ids and i not in missing]
    related: list[str] = []
    for uid in ids:
        for nid in sorted(index.neighbours(uid)):
            if nid not in read_ids and nid not in related:
                related.append(nid)
    footer = [f"[read {sum(i.tokens for i in items)} of {budget} tokens]"]
    if skipped:
        footer.append("Not read (over max_tokens): " + ", ".join(skipped))
    if missing:
        footer.append("Unknown ids: " + ", ".join(missing))
    if related:
        footer.append("Related ids: " + ", ".join(related[:10]))
    return "\n\n".join(blocks + ["\n".join(footer)])
