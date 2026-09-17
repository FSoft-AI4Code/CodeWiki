"""The incremental updater: Steps 1-6 in order, one record for everything."""

from __future__ import annotations

import logging
import os
import time
import traceback
from typing import Any

from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.cluster_modules import cluster_modules
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import pages as P
from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.change_report import (
    MODE_DELETE,
    LeafReport,
    active_set,
    build_reports,
    fallback_ratios,
    order_active,
)
from codewiki.src.be.updater.graph_diff import GraphDiff, diff_graphs
from codewiki.src.be.updater.graph_store import load_graph
from codewiki.src.be.updater.leaf_agent import LeafAgentRunner
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.record import (
    OUTCOME_DETECTOR_FAILURE,
    OUTCOME_FULL_FALLBACK,
    OUTCOME_INCREMENTAL,
    OUTCOME_NO_CHANGE,
    CallCost,
    UpdateRecord,
)
from codewiki.src.be.updater.reference_index import (
    build_reference_index,
    inverse,
    load_reference_index,
    save_reference_index,
)
from codewiki.src.be.updater.routing import RoutingAgent
from codewiki.src.be.updater.stale_scan import StaleScanner
from codewiki.src.be.updater.tree_repair import RepairResult, repair_tree
from codewiki.src.config import MODULE_TREE_FILENAME, Config
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)


class IncrementalUpdater:
    """Runs one incremental step over an existing docs directory.

    ``doc_generator`` is the normal ``DocumentationGenerator``; its
    ``generate_module_documentation`` regenerates whatever page is missing
    after the leaf agents ran (Step 6.1).
    """

    def __init__(
        self,
        config: Config,
        backend: LLMBackend,
        doc_generator: Any,
        opts: UpdateOptions,
    ) -> None:
        self.config = config
        self.backend = backend
        self.doc_generator = doc_generator
        self.opts = opts
        self.docs_dir = os.path.abspath(config.docs_dir)
        self.repo_name = os.path.basename(os.path.normpath(config.repo_path))
        self.whole_repo = False
        self._deleted_nodes: list[tuple[str, ...]] = []
        self.record = UpdateRecord(options=opts.to_dict())

    # ------------------------------------------------------------------ steps
    def _load_state(self, old_graph_path: str) -> tuple[dict[str, Node], dict[str, Any]]:
        old_graph = load_graph(old_graph_path)
        tree = file_manager.load_json(os.path.join(self.docs_dir, MODULE_TREE_FILENAME))
        if tree is None:
            raise FileNotFoundError(f"{MODULE_TREE_FILENAME} missing in {self.docs_dir}")
        if len(tree) == 0:
            self.whole_repo = True
            tree = T.virtual_whole_repo_tree(P.OVERVIEW_STEM, sorted(old_graph))
            self.record.detector_notes.append("whole-repository mode: one virtual leaf (overview)")
        return old_graph, tree

    def _module_path(self, path: tuple[str, ...]) -> list[str]:
        if self.whole_repo and path == (P.OVERVIEW_STEM,):
            return []
        return list(path)

    def _recluster(
        self,
        tree: dict[str, Any],
        flagged: list[tuple[str, ...]],
        new_graph: dict[str, Node],
        tracked_new: set[str],
    ) -> tuple[set[tuple[str, ...]], set[str]]:
        """Re-cluster the parent subtree of every growth-flagged leaf.

        Returns the set of unit paths whose pages must be regenerated and the
        set of page stems removed from disk."""
        reclustered: set[tuple[str, ...]] = set()
        removed_pages: set[str] = set()
        parents = sorted({p[:-1] for p in flagged if len(p) > 1}, key=len)
        done: set[tuple[str, ...]] = set()
        for parent in parents:
            if any(parent[: len(d)] == d for d in done):
                continue  # an ancestor was already re-clustered
            info = T.node_at(tree, parent)
            if info is None:
                continue
            comps = [c for c in T.components_of(info) if c in new_graph and c in tracked_new]
            if not comps:
                continue
            old_units = [p for p, _ in T.iter_nodes(info.get("children", {}), parent)]
            cluster_model = self.config.cluster_model or None
            started = time.time()
            err = None
            try:
                info["children"] = {}
                sub = cluster_modules(
                    comps,
                    new_graph,
                    self.config,
                    current_module_tree=tree,
                    current_module_name=parent[-1],
                    current_module_path=list(parent),
                    completer=lambda p, m=cluster_model: self.backend.complete(p, model=m),
                )
                if not sub:
                    info["children"] = {}
                    info["components"] = comps
            except Exception as e:  # noqa: BLE001 — recorded; subtree left as is
                err = f"{type(e).__name__}: {e}"
                logger.error("Re-clustering %s failed: %s", "/".join(parent), e)
                self.record.errors.append(f"recluster {'/'.join(parent)}: {err}")
            self.record.add_call(
                CallCost(
                    "recluster",
                    "/".join(parent),
                    time.time() - started,
                    getattr(self.backend, "last_usage", None),
                    err,
                )
            )
            if err:
                continue
            done.add(parent)
            for p in old_units:
                stem = p[-1]
                if P.page_exists(self.docs_dir, stem):
                    os.remove(P.page_path(self.docs_dir, stem))
                    removed_pages.add(stem)
                    self.record.pages_removed.append(stem)
            if P.page_exists(self.docs_dir, parent[-1]):
                os.remove(P.page_path(self.docs_dir, parent[-1]))
                removed_pages.add(parent[-1])
                self.record.pages_removed.append(parent[-1])
            new_info = T.node_at(tree, parent) or {}
            for p, _ in T.iter_nodes(new_info.get("children", {}), parent):
                reclustered.add(p)
            if T.is_leaf(new_info):
                reclustered.add(parent)
            self.record.reclustered.append(list(parent))
        return reclustered, removed_pages

    def _write_roles(
        self,
        report: LeafReport,
        new_tree: dict[str, Any],
        dep: dict[tuple[str, ...], set[tuple[str, ...]]],
        inv: dict[str, set[str]],
    ) -> dict[str, list[str]]:
        stem = report.page
        roles: dict[str, list[str]] = {}
        if report.mode != MODE_DELETE:
            roles[stem] = ["leaf"]
        for anc in T.ancestors(report.leaf_path):
            roles.setdefault(anc[-1], []).append("ancestor")
        if stem != P.OVERVIEW_STEM:
            roles.setdefault(P.OVERVIEW_STEM, []).append("ancestor")
        for d in sorted(dep.get(report.leaf_path, ())):
            roles.setdefault(d[-1], []).append("dependent")
        for page in sorted(inv.get(stem, ())):
            roles.setdefault(page, []).append("referrer")
        existing = set(P.list_pages(self.docs_dir))
        doomed = {p[-1] for p in self._deleted_nodes} - {stem}
        return {
            p: r
            for p, r in roles.items()
            if (p in existing or (p == stem and report.mode != MODE_DELETE)) and p not in doomed
        }

    # ------------------------------------------------------------------- main
    async def run(
        self,
        old_graph_path: str | None,
        new_graph: dict[str, Node],
        leaf_nodes: list[str],
        revision: dict[str, Any],
    ) -> UpdateRecord:
        rec = self.record
        rec.revision = dict(revision)
        t0 = time.time()
        try:
            return await self._run(old_graph_path, new_graph, leaf_nodes)
        finally:
            rec.wall_seconds = round(time.time() - t0, 2)
            rec.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            try:
                rec.save(self.docs_dir)
            except OSError as e:
                logger.error("Could not save update record: %s", e)

    async def _run(
        self, old_graph_path: str | None, new_graph: dict[str, Node], leaf_nodes: list[str]
    ) -> UpdateRecord:
        rec = self.record
        # ---- Step 0: load what the previous build left behind
        try:
            if not old_graph_path or not os.path.exists(old_graph_path):
                raise FileNotFoundError("previous dependency graph not found")
            old_graph, old_tree = self._load_state(old_graph_path)
        except Exception as e:  # noqa: BLE001 — a detector failure is an outcome, not a crash
            rec.outcome = OUTCOME_DETECTOR_FAILURE
            rec.errors.append(f"load previous state: {type(e).__name__}: {e}")
            logger.warning("Incremental update impossible (%s); falling back to a full build", e)
            return rec

        ref_index = load_reference_index(self.docs_dir)
        if ref_index is None:
            ref_index = build_reference_index(
                self.docs_dir, old_graph, None if self.whole_repo else old_tree
            )
            rec.detector_notes.append(
                "reference index rebuilt from pages (none saved by previous build)"
            )

        # ---- Step 1: diff
        diff: GraphDiff = diff_graphs(old_graph, new_graph, self.opts)
        rec.diff = diff.to_dict()
        if diff.is_empty:
            rec.outcome = OUTCOME_NO_CHANGE
            logger.info("No component-level change detected; documentation is up to date")
            return rec

        # ---- Step 2: repair
        tracked_new = set(leaf_nodes) | (T.tracked_ids(old_tree) & set(new_graph))
        router = (
            RoutingAgent(self.backend, self.docs_dir, rec, self.config.cluster_model or None)
            if self.opts.use_routing_agent
            else None
        )
        repair: RepairResult = repair_tree(
            old_tree, diff, new_graph, tracked_new, self.opts, route_orphans=router
        )
        new_tree = repair.tree
        reclustered: set[tuple[str, ...]] = set()
        removed_pages: set[str] = set()
        if self.opts.use_growth_recluster and repair.growth_flagged and not self.whole_repo:
            reclustered, removed_pages = self._recluster(
                new_tree, repair.growth_flagged, new_graph, tracked_new
            )
        rec.repair = repair.to_dict()
        self._deleted_nodes = list(repair.deleted_nodes)

        # ---- Step 3: reports
        reports = build_reports(
            diff,
            old_tree,
            new_tree,
            old_graph,
            new_graph,
            ref_index,
            repair,
            self.opts,
            reclustered,
        )
        active = active_set(reports)
        rec.reports = {"/".join(p): r.to_dict() for p, r in reports.items() if p in active}

        # ---- Step 4: fallback check
        ratios = fallback_ratios(reports, new_tree, repair, reclustered)
        ratios["tau_full"] = self.opts.tau_full
        ratios["tau_tree"] = self.opts.tau_tree
        ratios["fired"] = (
            ratios["r_leaf"] >= self.opts.tau_full or ratios["r_tree"] >= self.opts.tau_tree
        )
        if self.whole_repo and ratios["fired"]:
            # One virtual leaf: any change is 100% active by construction, and
            # patching that single page is exactly the incremental step.
            ratios["fired"] = False
            ratios["note"] = "whole-repository mode: fallback rule not applied"
        rec.fallback = ratios
        if ratios["fired"]:
            rec.outcome = OUTCOME_FULL_FALLBACK
            logger.warning(
                "Fallback to full build: r_leaf=%.2f (tau %.2f), r_tree=%.2f (tau %.2f)",
                ratios["r_leaf"],
                self.opts.tau_full,
                ratios["r_tree"],
                self.opts.tau_tree,
            )
            return rec

        # Persist the repaired tree so the normal pipeline and the agents see it.
        if not self.whole_repo:
            file_manager.save_json(new_tree, os.path.join(self.docs_dir, MODULE_TREE_FILENAME))

        # ---- Step 5: sequential leaf agents
        order = order_active(active, new_tree, new_graph)
        dep = T.leaf_dependents(new_tree, new_graph)
        inv = inverse(ref_index)
        rec.active = [
            {"leaf": "/".join(p), "page": reports[p].page, "mode": reports[p].mode, "order": i}
            for i, p in enumerate(order)
        ]
        runner = LeafAgentRunner(
            self.config, self.backend, self.docs_dir, new_graph, new_tree, diff, self.opts, rec
        )
        for path in order:
            report = reports[path]
            roles = self._write_roles(report, new_tree, dep, inv)
            info = T.node_at(new_tree, path) or {}
            component_ids = [c for c in T.components_of(info) if c in new_graph]
            if self.whole_repo:
                component_ids = [c for c in leaf_nodes if c in new_graph]
            report.leaf_path = tuple(path)
            try:
                logger.info(
                    "Updating leaf %s (mode=%s, write set=%s)",
                    "/".join(path),
                    report.mode,
                    sorted(roles),
                )
                if self.whole_repo:
                    # The virtual leaf is the overview page with an empty module path.
                    await runner.run(
                        _WholeRepoReport(report, P.OVERVIEW_STEM), roles, component_ids
                    )
                else:
                    await runner.run(report, roles, component_ids)
            except Exception as e:  # noqa: BLE001 — one failed leaf must not abort the update
                rec.errors.append(f"leaf {'/'.join(path)}: {type(e).__name__}: {e}")
                logger.error("Leaf %s failed: %s\n%s", "/".join(path), e, traceback.format_exc())

        # ---- Step 6.1: generate any page still missing (new parents, re-clustered subtrees, root)
        before = P.page_hashes(self.docs_dir)
        started = time.time()
        err = None
        try:
            await self.doc_generator.generate_module_documentation(new_graph, leaf_nodes)
        except Exception as e:  # noqa: BLE001 — recorded
            err = f"{type(e).__name__}: {e}"
            rec.errors.append(f"missing pages: {err}")
            logger.error("Generating missing pages failed: %s", e)
        created = sorted(P.changed_pages(before, P.page_hashes(self.docs_dir)))
        rec.add_call(
            CallCost("missing_pages", ",".join(created) or "-", time.time() - started, None, err)
        )
        rec.pages_written.extend(created)

        # ---- Step 6.2: stale-name scan over pages not written this round
        removed_all = set(rec.pages_removed) | removed_pages
        if self.opts.use_stale_scan:
            replacements = {stem: self._nearest_page(stem, new_tree) for stem in removed_all}
            scanner = StaleScanner(
                self.config, self.backend, self.docs_dir, new_graph, new_tree, rec
            )
            rec.stale_scan = await scanner.run(
                diff, old_graph, set(rec.pages_written), removed_all, replacements
            )

        # ---- Step 6.3: rebuild the reference index
        new_index = build_reference_index(
            self.docs_dir, new_graph, None if self.whole_repo else new_tree
        )
        save_reference_index(new_index, self.docs_dir)
        rec.outcome = OUTCOME_INCREMENTAL
        return rec

    def _nearest_page(self, removed_stem: str, new_tree: dict[str, Any]) -> str | None:
        """Best existing page to redirect a dangling link to: the removed module's
        nearest surviving ancestor, else the overview."""
        for path, _ in T.iter_nodes(new_tree):
            if path[-1] == removed_stem:
                return None
        # Look the removed module up in the record's deleted nodes to find its parent.
        for p in self.record.repair.get("deleted_nodes", []):
            if p and p[-1] == removed_stem:
                for anc in reversed(p[:-1]):
                    if P.page_exists(self.docs_dir, anc):
                        return anc
        return P.OVERVIEW_STEM if P.page_exists(self.docs_dir, P.OVERVIEW_STEM) else None


class _WholeRepoReport:
    """Proxy so the leaf agent treats the whole-repo virtual leaf as the
    overview page with an empty module path."""

    def __init__(self, report: LeafReport, page: str) -> None:
        self._r = report
        self._page = page

    def __getattr__(self, item: str) -> Any:
        return getattr(self._r, item)

    @property
    def page(self) -> str:
        return self._page

    @property
    def leaf_path(self) -> tuple[str, ...]:
        return tuple()
