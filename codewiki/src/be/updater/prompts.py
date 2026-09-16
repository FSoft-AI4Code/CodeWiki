"""Prompts for the incremental updater's agents.

Three agents: the per-leaf editing agent (Step 5), the orphan routing agent
(Step 2, rule 4) and the stale-name fixer (Step 6). Every agent that edits
pages must end its answer with a fenced JSON verdict block.
"""

from __future__ import annotations

import json
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.prompt_template import _fence_language, _format_module_tree_str
from codewiki.src.be.updater.change_report import LeafReport
from codewiki.src.be.updater.graph_diff import GraphDiff

VERDICT_VALUES = ("no-op", "patch", "rewrite")

UPDATE_LEAF_SYSTEM_PROMPT = """
<ROLE>
You maintain an existing documentation wiki for a code repository. The code moved to a new
revision. You receive a precise report of what changed for ONE module ("the leaf") and you
bring the affected documentation pages up to date with the smallest correct edits.
</ROLE>

<RULES>
1. You may edit ONLY the pages in the WRITE SET below. The editor tool refuses anything else.
   If another page needs a change, say so in your final verdict instead of trying.
2. Prefer surgical edits with `str_replace_editor` (`str_replace` / `insert`). Keep every
   sentence that is still true word for word. Do not reflow, restyle, or "improve" prose that
   the report does not touch.
3. Per page role:
   - the LEAF PAGE ({leaf_name}.md): update sections, tables and diagrams that describe changed
     components; add new components; remove deleted ones. If the change is so large that the
     page is better rebuilt from scratch, do NOT rebuild it yourself: return verdict "rewrite"
     for this page and leave it untouched (the normal module agent will regenerate it).
   - an ANCESTOR page: change only where it summarizes this leaf or lists its children.
   - a DEPENDENT page: change only where it describes the contract of a component listed under
     UP (a signature that moved, a component that was deleted or renamed) or a call that no
     longer exists.
   - a REFERRER page: change only where it names a component or page listed under REFCH.
4. Use `read_code_components` to read the fresh code of any component id when the diff alone is
   not enough. Use `str_replace_editor` with `working_dir="docs"` and `command="view"` to read a
   page before editing it.
5. Mermaid diagrams must stay valid. Links between pages are relative: `[text](page.md)`.
</RULES>

<OUTPUT>
When you are done, end your answer with exactly one fenced JSON block:
```json
{{"verdicts": {{"<page>.md": {{"verdict": "no-op|patch|rewrite", "reason": "<one line>"}}, ...}},
 "notes": "<anything the maintainers should know, or empty>"}}
```
Give a verdict for EVERY page in the write set. "patch" means you edited it; "no-op" means it is
already correct; "rewrite" is allowed only for the leaf page.
</OUTPUT>
{custom_instructions}
""".strip()

UPDATE_LEAF_USER_PROMPT = """
Update the documentation for the module `{leaf_name}` (mode: {mode}).

{mode_note}

<WRITE_SET>
{write_set}
</WRITE_SET>

<CHANGE_REPORT>
{report}
</CHANGE_REPORT>

<MODULE_TREE>
{module_tree}
</MODULE_TREE>

<LEAF_COMPONENTS>
{leaf_components}
</LEAF_COMPONENTS>

<CURRENT_LEAF_PAGE path="{leaf_name}.md">
{leaf_page}
</CURRENT_LEAF_PAGE>

Work through the write set page by page, then end with the JSON verdict block.
""".strip()

MODE_NOTES = {
    "edit": (
        "The leaf page exists. Decide per page: patch in place, no-op, or (leaf page only) "
        "'rewrite' if a fresh page would be better than patching."
    ),
    "create": (
        "The leaf page was just generated from scratch and must NOT be changed here. Your job is "
        "the related pages: make ancestors list and summarize the new module, and fix any "
        "referrer that should now point at it."
    ),
    "delete": (
        "This module no longer exists and its page has been removed. Update the related pages: "
        "drop it from ancestor summaries and child lists, and remove or redirect every link or "
        "mention of it on the referrer pages."
    ),
    "related_only": (
        "The leaf page was regenerated from scratch by the normal module agent and must NOT be "
        "changed here. Update the related pages so they match the regenerated leaf page."
    ),
}

ROUTING_SYSTEM_PROMPT = """
You place newly added code components into an existing module tree of a documentation wiki.
Answer with JSON only.
""".strip()

ROUTING_USER_PROMPT = """
The repository changed and these new components could not be placed by rules (same file,
same directory, or majority of graph neighbours). Place each one.

<MODULE_TREE>
{module_tree}
</MODULE_TREE>

<ORPHANS>
{orphans}
</ORPHANS>

For each orphan choose exactly one action:
- "place": add it to an existing leaf module ("leaf": exact module name from the tree).
- "create": create a new leaf module ("new_leaf": short snake_case name, "parent": exact name of an
  existing module that has children, or null for top level). Use the same new_leaf name for
  orphans that belong together; a new subsystem landing in one commit should become one new leaf.
- "untracked": leave it out of the wiki (tests, throwaway scripts, trivial helpers).

Return exactly one fenced JSON block:
```json
{{"decisions": [{{"component_id": "...", "action": "place|create|untracked", "leaf": "...",
                 "new_leaf": "...", "parent": "...", "reason": "<one line>"}}]}}
```
""".strip()

STALE_FIX_SYSTEM_PROMPT = """
You fix stale references in ONE documentation page after a code change. Make the smallest edits
that remove or correct the stale items; everything else on the page stays word for word.
End your answer with a fenced JSON block: {"verdicts": {"<page>.md": {"verdict": "patch|no-op",
"reason": "..."}}}.
""".strip()

STALE_FIX_USER_PROMPT = """
Page to fix: `{page}.md` (view it with str_replace_editor, working_dir="docs").

<STALE_ITEMS>
{items}
</STALE_ITEMS>

Rules: a renamed component gets its new id/name; a deleted component or page is removed from
prose, tables, lists and diagrams (or the sentence is rephrased so it stays true); a link to a
page that no longer exists is removed or repointed to the page listed as its replacement.
""".strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n... [clipped] ...\n" + text[-(limit // 2) :]


def render_record(diff: GraphDiff, cid: str, max_chars: int = 40_000) -> str:
    rec = diff.record_for(cid)
    if rec is None:
        return f"- {cid}"
    lines = [f"### {cid}  [{rec.change_class}]"]
    if rec.change_class == "renamed":
        lines.append(f"renamed from `{rec.old_id}` to `{rec.new_id}`")
    if rec.signature_changed or rec.change_class == "interface":
        lines.append(f"signature before: {json.dumps(rec.old_signature)}")
        lines.append(f"signature after:  {json.dumps(rec.new_signature)}")
    if rec.edges_added or rec.edges_removed:
        lines.append(f"now uses: {rec.edges_added}; no longer uses: {rec.edges_removed}")
    if rec.diff:
        lines.append("```diff\n" + _clip(rec.diff, max_chars) + "\n```")
    return "\n".join(lines)


def render_report(report: LeafReport, diff: GraphDiff) -> str:
    parts: list[str] = []
    if report.own:
        parts.append("## OWN — components of this leaf that changed")
        parts += [render_record(diff, c) for c in report.own]
    if report.up:
        parts.append(
            "## UP — components outside this leaf that its code uses and whose contract moved"
        )
        parts += [render_record(diff, c) for c in report.up]
    if report.context:
        parts.append("## CONTEXT — changed components not tracked by any module, next to this leaf")
        parts += [render_record(diff, c) for c in report.context]
    if report.refch:
        parts.append("## REFCH — things this leaf's page refers to that changed or vanished")
        parts += [f"- {x}" for x in report.refch]
    tree_lines = []
    if report.entered:
        tree_lines.append(f"- components that entered this module: {report.entered}")
    if report.left:
        tree_lines.append(f"- components that left this module: {report.left}")
    if report.children_added:
        tree_lines.append(f"- child modules added: {report.children_added}")
    if report.children_removed:
        tree_lines.append(f"- child modules removed: {report.children_removed}")
    if report.reclustered:
        tree_lines.append("- this subtree was re-clustered")
    if tree_lines:
        parts.append("## TREE — structural changes")
        parts += tree_lines
    return (
        "\n".join(parts)
        if parts
        else "(no direct changes; this leaf is active for structural reasons)"
    )


def render_write_set(roles: dict[str, list[str]]) -> str:
    """``roles``: page stem -> list of roles (leaf/ancestor/dependent/referrer)."""
    lines = []
    for page, rs in roles.items():
        lines.append(f"- {page}.md  ({', '.join(rs)})")
    return "\n".join(lines) if lines else "(empty)"


def render_leaf_components(
    component_ids: list[str],
    graph: dict[str, Node],
    changed: set[str],
    max_code_chars: int = 60_000,
) -> str:
    """List every component of the leaf; inline fresh code only for changed ones."""
    lines = ["Components of this module (fresh revision):"]
    for cid in component_ids:
        node = graph.get(cid)
        if node is None:
            lines.append(f"- {cid}  (no longer in the code graph)")
            continue
        sig = ", ".join(node.parameters or [])
        lines.append(
            f"- {cid}  [{node.component_type}] ({sig}) lines {node.start_line}-{node.end_line}"
        )
    budget = max_code_chars
    shown = 0
    for cid in component_ids:
        node = graph.get(cid)
        if node is None or cid not in changed or not node.source_code:
            continue
        code = node.source_code
        if budget <= 0:
            lines.append(f"\n(code of {cid} omitted for length; use read_code_components)")
            continue
        code = _clip(code, budget)
        budget -= len(code)
        shown += 1
        lines.append(
            f'\n<CODE id="{cid}">\n```{_fence_language(node.relative_path)}\n{code}\n```\n</CODE>'
        )
    if shown == 0:
        lines.append("\n(no changed component code inlined; use read_code_components as needed)")
    return "\n".join(lines)


def render_tree_outline(tree: dict[str, Any], current: str | None) -> str:
    return _format_module_tree_str(tree, current, include_components=False)


def format_update_system_prompt(leaf_name: str, custom_instructions: str | None) -> str:
    extra = (
        f"\n<CUSTOM_INSTRUCTIONS>\n{custom_instructions}\n</CUSTOM_INSTRUCTIONS>"
        if custom_instructions
        else ""
    )
    return UPDATE_LEAF_SYSTEM_PROMPT.format(leaf_name=leaf_name, custom_instructions=extra)


def format_update_user_prompt(
    *,
    leaf_name: str,
    mode: str,
    roles: dict[str, list[str]],
    report: LeafReport,
    diff: GraphDiff,
    tree: dict[str, Any],
    component_ids: list[str],
    graph: dict[str, Node],
    leaf_page_text: str | None,
) -> str:
    changed = set(report.own)
    return UPDATE_LEAF_USER_PROMPT.format(
        leaf_name=leaf_name,
        mode=mode,
        mode_note=MODE_NOTES.get(mode, MODE_NOTES["edit"]),
        write_set=render_write_set(roles),
        report=render_report(report, diff),
        module_tree=render_tree_outline(tree, leaf_name),
        leaf_components=render_leaf_components(component_ids, graph, changed),
        leaf_page=(
            leaf_page_text if leaf_page_text is not None else "(page does not exist / was removed)"
        ),
    )


def format_routing_prompt(
    tree_outline: str,
    orphans: list[str],
    graph: dict[str, Node],
    neighbours: dict[str, list[str]],
    max_code_chars: int = 6_000,
) -> str:
    blocks = []
    for cid in orphans:
        node = graph[cid]
        code = _clip(node.source_code or "", max_code_chars)
        nb = neighbours.get(cid) or []
        blocks.append(
            f'<ORPHAN id="{cid}" type="{node.component_type}" file="{node.relative_path}">\n'
            f"neighbour modules in the code graph: {nb if nb else 'none'}\n"
            f"```{_fence_language(node.relative_path)}\n{code}\n```\n</ORPHAN>"
        )
    return ROUTING_USER_PROMPT.format(module_tree=tree_outline, orphans="\n".join(blocks))


def format_stale_prompt(page: str, items: list[dict[str, Any]]) -> str:
    lines = []
    for it in items:
        kind = it.get("kind")
        if kind == "renamed":
            lines.append(f"- renamed component: `{it['old']}` is now `{it['new']}`")
        elif kind == "deleted":
            lines.append(f"- deleted component: `{it['old']}` no longer exists")
        elif kind == "deleted_page":
            repl = it.get("replacement")
            lines.append(
                f"- dangling link: `{it['old']}.md` no longer exists"
                + (f"; nearest existing page: `{repl}.md`" if repl else "")
            )
        else:
            lines.append(f"- {json.dumps(it)}")
    return STALE_FIX_USER_PROMPT.format(page=page, items="\n".join(lines))
