"""Step 5: one editing agent per active leaf, restricted to a write set."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import pages as P
from codewiki.src.be.updater.change_report import MODE_CREATE, MODE_DELETE, MODE_EDIT, LeafReport
from codewiki.src.be.updater.graph_diff import GraphDiff
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.prompts import format_update_system_prompt, format_update_user_prompt
from codewiki.src.be.updater.record import CallCost, PageVerdict, UpdateRecord
from codewiki.src.be.updater.verdicts import parse_verdicts
from codewiki.src.config import Config

logger = logging.getLogger(__name__)


class LeafAgentRunner:
    def __init__(
        self,
        config: Config,
        backend: LLMBackend,
        docs_dir: str,
        graph: dict[str, Node],
        tree: dict[str, Any],
        diff: GraphDiff,
        opts: UpdateOptions,
        record: UpdateRecord,
    ) -> None:
        self.config = config
        self.backend = backend
        self.docs_dir = docs_dir
        self.graph = graph
        self.tree = tree
        self.diff = diff
        self.opts = opts
        self.record = record
        self.custom_instructions = config.get_prompt_addition()

    # ------------------------------------------------------------------ utils
    def _deps(
        self, leaf_name: str, module_path: list[str], allowed: set[str] | None
    ) -> CodeWikiDeps:
        return CodeWikiDeps(
            absolute_docs_path=self.docs_dir,
            absolute_repo_path=str(os.path.abspath(self.config.repo_path)),
            registry={},
            components=self.graph,
            path_to_current_module=module_path,
            current_module_name=leaf_name,
            module_tree=self.tree,
            max_depth=self.config.max_depth,
            current_depth=1,
            config=self.config,
            custom_instructions=self.custom_instructions,
            allowed_write_paths=allowed,
        )

    def _remove_page(self, stem: str, by_leaf: str, reason: str) -> None:
        path = P.page_path(self.docs_dir, stem)
        if os.path.exists(path):
            os.remove(path)
            self.record.pages_removed.append(stem)
            self.record.add_verdict(PageVerdict(stem, "delete", reason, by_leaf, True))

    async def _regenerate_leaf(
        self, leaf_name: str, module_path: list[str], component_ids: list[str], why: str
    ) -> None:
        """Delete the page (if any) and let the normal module agent write it anew."""
        path = P.page_path(self.docs_dir, leaf_name)
        existed = os.path.exists(path)
        if existed:
            os.remove(path)
        started = time.time()
        err = None
        try:
            await self.backend.run_module_agent(
                module_name=leaf_name,
                components=self.graph,
                core_component_ids=component_ids,
                module_path=module_path,
                working_dir=self.docs_dir,
            )
        except Exception as e:  # noqa: BLE001 — recorded, the run continues
            err = f"{type(e).__name__}: {e}"
            logger.error("Regenerating %s failed: %s", leaf_name, e)
        self.record.add_call(
            CallCost(
                "rewrite" if existed else "create",
                leaf_name,
                time.time() - started,
                getattr(self.backend, "last_usage", None),
                err,
            )
        )
        self.record.add_verdict(
            PageVerdict(
                leaf_name, "rewrite" if existed else "create", why, leaf_name, os.path.exists(path)
            )
        )
        if os.path.exists(path):
            self.record.pages_written.append(leaf_name)

    async def _run_editing_agent(
        self,
        leaf_name: str,
        module_path: list[str],
        mode: str,
        roles: dict[str, list[str]],
        report: LeafReport,
        component_ids: list[str],
        kind: str = "leaf_agent",
    ) -> dict[str, dict[str, str]]:
        """Run the update agent over ``roles`` (page -> roles). Returns verdicts."""
        if not roles:
            return {}
        allowed = {P.page_path(self.docs_dir, stem) for stem in roles}
        deps = self._deps(leaf_name, module_path, allowed)
        system_prompt = format_update_system_prompt(leaf_name, self.custom_instructions)
        user_prompt = format_update_user_prompt(
            leaf_name=leaf_name,
            mode=mode,
            roles=roles,
            report=report,
            diff=self.diff,
            tree=self.tree,
            component_ids=component_ids,
            graph=self.graph,
            leaf_page_text=P.read_page(self.docs_dir, leaf_name) if leaf_name in roles else None,
        )
        before = P.page_hashes(self.docs_dir)
        started = time.time()
        err = None
        text = ""
        usage = None
        try:
            reply = await self.backend.run_update_agent(system_prompt, user_prompt, deps)
            text, usage = reply.text, reply.usage
        except Exception as e:  # noqa: BLE001 — recorded, the run continues
            err = f"{type(e).__name__}: {e}"
            logger.error("Update agent for %s failed: %s", leaf_name, e)
        seconds = time.time() - started
        self.record.add_call(CallCost(kind, leaf_name, seconds, usage, err))
        after = P.page_hashes(self.docs_dir)
        changed = P.changed_pages(before, after)
        verdicts, notes = parse_verdicts(text)
        if notes:
            self.record.detector_notes.append(f"[{leaf_name}] agent notes: {notes[:500]}")
        for stem in roles:
            v = verdicts.get(stem)
            on_disk = stem in changed
            if v is None:
                verdict = "patch" if on_disk else "no-op"
                reason = "no verdict returned by agent" + (
                    " (page changed on disk)" if on_disk else ""
                )
            else:
                verdict, reason = v["verdict"], v["reason"]
                if verdict not in ("no-op", "patch", "rewrite"):
                    verdict = "patch" if on_disk else "no-op"
                if verdict == "no-op" and on_disk:
                    verdict, reason = "patch", (reason + " [page changed on disk]").strip()
                if verdict == "patch" and not on_disk:
                    reason = (reason + " [no change on disk]").strip()
            self.record.add_verdict(PageVerdict(stem, verdict, reason, leaf_name, on_disk))
            if on_disk:
                self.record.pages_written.append(stem)
            verdicts[stem] = {"verdict": verdict, "reason": reason}
        for stem in changed - set(roles):
            self.record.write_set_violations.append(
                {"leaf": leaf_name, "page": stem, "note": "changed outside the write set"}
            )
            logger.error("Write-set violation: %s changed while updating %s", stem, leaf_name)
        return verdicts

    # ------------------------------------------------------------------- main
    async def run(
        self,
        report: LeafReport,
        write_roles: dict[str, list[str]],
        component_ids: list[str],
    ) -> None:
        """``write_roles``: page stem -> roles, restricted to pages that exist
        (plus the leaf page itself in edit mode)."""
        leaf_name = report.page
        module_path = list(report.leaf_path)
        related = {p: r for p, r in write_roles.items() if p != leaf_name}
        self.record.write_sets[leaf_name] = sorted(write_roles)

        if report.mode == MODE_DELETE:
            self._remove_page(leaf_name, leaf_name, "module removed from the tree")
            if related and self.opts.agent_patches_related:
                await self._run_editing_agent(
                    leaf_name, module_path, MODE_DELETE, related, report, component_ids
                )
            elif related:
                self._legacy_invalidate(related, leaf_name)
            return

        if report.mode == MODE_CREATE or not P.page_exists(self.docs_dir, leaf_name):
            await self._regenerate_leaf(leaf_name, module_path, component_ids, "new module page")
            if related and self.opts.agent_patches_related:
                await self._run_editing_agent(
                    leaf_name, module_path, MODE_CREATE, related, report, component_ids
                )
            elif related:
                self._legacy_invalidate(related, leaf_name)
            return

        # edit mode
        if not self.opts.agent_may_patch_leaf:
            await self._regenerate_leaf(
                leaf_name, module_path, component_ids, "rewrite_always (ablation rung)"
            )
            if related and self.opts.agent_patches_related:
                await self._run_editing_agent(
                    leaf_name, module_path, "related_only", related, report, component_ids
                )
            elif related:
                self._legacy_invalidate(related, leaf_name)
            return

        roles = {leaf_name: ["leaf"], **related}
        verdicts = await self._run_editing_agent(
            leaf_name, module_path, MODE_EDIT, roles, report, component_ids
        )
        own = verdicts.get(leaf_name, {})
        if own.get("verdict") == "rewrite":
            await self._regenerate_leaf(
                leaf_name, module_path, component_ids, own.get("reason", "agent chose rewrite")
            )

    def _legacy_invalidate(self, related: dict[str, list[str]], by_leaf: str) -> None:
        """Rung 1 behaviour: ancestors are deleted and regenerated later; other
        related pages are left alone."""
        for stem, roles in related.items():
            if "ancestor" in roles:
                self._remove_page(stem, by_leaf, "ancestor invalidated (rung 1)")
