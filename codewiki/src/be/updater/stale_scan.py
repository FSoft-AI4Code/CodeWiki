"""Step 6.2: scan pages not written this round for stale ids, names and links."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import pages as P
from codewiki.src.be.updater.graph_diff import GraphDiff
from codewiki.src.be.updater.prompts import STALE_FIX_SYSTEM_PROMPT, format_stale_prompt
from codewiki.src.be.updater.reference_index import unique_names_of
from codewiki.src.be.updater.record import CallCost, PageVerdict, UpdateRecord
from codewiki.src.be.updater.verdicts import parse_verdicts
from codewiki.src.config import Config

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(r"\]\(\s*<?([^)\s>#]+\.md)(?:#[^)]*)?>?\s*\)")


def find_stale_items(
    text: str,
    diff: GraphDiff,
    old_graph: dict[str, Node],
    existing_pages: set[str],
    removed_pages: set[str],
    page_replacements: dict[str, str | None],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    unique_gone = unique_names_of(old_graph, set(diff.deleted) | set(diff.renamed.keys()))
    for old_id, new_id in diff.renamed.items():
        if old_id in text:
            items.append({"kind": "renamed", "old": old_id, "new": new_id})
        else:
            old_name = old_graph[old_id].name if old_id in old_graph else None
            new_name = new_id.split("::", 1)[-1]
            if (
                old_name
                and old_name in unique_gone
                and old_name != new_name
                and re.search(rf"`{re.escape(old_name)}`", text)
            ):
                items.append({"kind": "renamed", "old": old_name, "new": new_name})
    for cid in diff.deleted:
        if cid in text:
            items.append({"kind": "deleted", "old": cid})
        else:
            name = old_graph[cid].name if cid in old_graph else None
            if name and name in unique_gone and re.search(rf"`{re.escape(name)}`", text):
                items.append({"kind": "deleted", "old": name})
    for m in _LINK_RE.finditer(text):
        stem = os.path.splitext(os.path.basename(m.group(1)))[0]
        if stem in removed_pages or (stem not in existing_pages and "/" not in m.group(1)):
            items.append(
                {"kind": "deleted_page", "old": stem, "replacement": page_replacements.get(stem)}
            )
    # dedupe
    seen = set()
    out = []
    for it in items:
        key = (it["kind"], it["old"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


class StaleScanner:
    def __init__(
        self,
        config: Config,
        backend: LLMBackend,
        docs_dir: str,
        graph: dict[str, Node],
        tree: dict[str, Any],
        record: UpdateRecord,
    ):
        self.config = config
        self.backend = backend
        self.docs_dir = docs_dir
        self.graph = graph
        self.tree = tree
        self.record = record

    async def run(
        self,
        diff: GraphDiff,
        old_graph: dict[str, Node],
        skip_pages: set[str],
        removed_pages: set[str],
        page_replacements: dict[str, str | None],
    ) -> dict[str, Any]:
        existing = set(P.list_pages(self.docs_dir))
        scanned, hits, fixed = 0, {}, []
        for stem in sorted(existing - skip_pages):
            text = P.read_page(self.docs_dir, stem) or ""
            scanned += 1
            items = find_stale_items(
                text, diff, old_graph, existing, removed_pages, page_replacements
            )
            if not items:
                continue
            hits[stem] = items
            await self._fix(stem, items)
            fixed.append(stem)
        summary = {"scanned": scanned, "pages_with_hits": hits, "fixed": fixed}
        logger.info("Stale scan: %d pages scanned, %d with hits", scanned, len(hits))
        return summary

    async def _fix(self, stem: str, items: list[dict[str, Any]]) -> None:
        deps = CodeWikiDeps(
            absolute_docs_path=self.docs_dir,
            absolute_repo_path=str(os.path.abspath(self.config.repo_path)),
            registry={},
            components=self.graph,
            path_to_current_module=[],
            current_module_name=stem,
            module_tree=self.tree,
            max_depth=self.config.max_depth,
            current_depth=1,
            config=self.config,
            custom_instructions=self.config.get_prompt_addition(),
            allowed_write_paths={P.page_path(self.docs_dir, stem)},
        )
        before = P.page_hashes(self.docs_dir)
        started = time.time()
        err = None
        text = ""
        usage = None
        try:
            reply = await self.backend.run_update_agent(
                STALE_FIX_SYSTEM_PROMPT, format_stale_prompt(stem, items), deps
            )
            text, usage = reply.text, reply.usage
        except Exception as e:  # noqa: BLE001 — recorded; page stays as is
            err = f"{type(e).__name__}: {e}"
            logger.error("Stale fix for %s failed: %s", stem, e)
        self.record.add_call(CallCost("stale_fix", stem, time.time() - started, usage, err))
        changed = stem in P.changed_pages(before, P.page_hashes(self.docs_dir))
        verdicts, _ = parse_verdicts(text)
        v = verdicts.get(stem, {})
        verdict = "patch" if changed else v.get("verdict", "no-op")
        self.record.add_verdict(
            PageVerdict(stem, verdict, v.get("reason", "stale-name scan"), "stale_scan", changed)
        )
        if changed:
            self.record.pages_written.append(stem)
