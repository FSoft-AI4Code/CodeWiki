"""Step 2: repair the module tree after a component-level diff.

Order: renames are rewritten in place, deleted components are removed
(empty leaves disappear), added components are routed by rules 1-3, and
what the rules cannot place is handed to a routing callable (rule 4).
The growth check only *flags* leaves; re-clustering is the orchestrator's
job because it needs the clustering step and an LLM.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.module_naming import (
    collect_module_tree_names,
    resolve_unique_name,
    sanitize_module_name,
)
from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.graph_diff import GraphDiff
from codewiki.src.be.updater.options import UpdateOptions

logger = logging.getLogger(__name__)

RULE_SAME_FILE = "1:same_file"
RULE_SAME_DIR = "2:same_dir"
RULE_NEIGHBOR = "3:neighbor_majority"
RULE_AGENT = "4:routing_agent"
RULE_UNTRACKED = "untracked"


@dataclass
class RoutingDecision:
    component_id: str
    rule: str
    leaf_path: tuple[str, ...] | None = None  # None = left untracked
    new_leaf: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["leaf_path"] = list(self.leaf_path) if self.leaf_path else None
        return d


@dataclass
class RepairResult:
    tree: dict[str, Any]
    renamed: dict[str, str] = field(default_factory=dict)
    removed: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    routing: list[RoutingDecision] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    deleted_nodes: list[tuple[str, ...]] = field(default_factory=list)
    created_leaves: list[tuple[str, ...]] = field(default_factory=list)
    growth: dict[tuple[str, ...], float] = field(default_factory=dict)
    growth_flagged: list[tuple[str, ...]] = field(default_factory=list)
    entered: dict[tuple[str, ...], list[str]] = field(default_factory=dict)
    left: dict[tuple[str, ...], list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "renamed": dict(sorted(self.renamed.items())),
            "removed": [{"component_id": c, "leaf_path": list(p)} for c, p in self.removed],
            "routing": [r.to_dict() for r in self.routing],
            "orphans": sorted(self.orphans),
            "deleted_nodes": [list(p) for p in self.deleted_nodes],
            "created_leaves": [list(p) for p in self.created_leaves],
            "growth": {"/".join(p): round(g, 4) for p, g in self.growth.items()},
            "growth_flagged": [list(p) for p in self.growth_flagged],
        }


# Signature of the rule-4 callable: (orphan ids, context) -> decisions.
OrphanRouter = Callable[[list[str], dict[str, Any]], list[RoutingDecision]]


def _dirname(rel: str) -> str:
    return os.path.dirname(rel.replace("\\", "/"))


def _route_by_rules(
    cid: str,
    node: Node,
    owner: dict[str, tuple[str, ...]],
    new_graph: dict[str, Node],
    opts: UpdateOptions,
) -> RoutingDecision | None:
    # Rule 1: same file already tracked.
    same_file = Counter(
        owner[c]
        for c in owner
        if c in new_graph and new_graph[c].relative_path == node.relative_path
    )
    if same_file:
        leaf, n = same_file.most_common(1)[0]
        return RoutingDecision(cid, RULE_SAME_FILE, leaf, detail=f"{n} tracked components in file")
    # Rule 2: same directory, single leaf.
    d = _dirname(node.relative_path)
    same_dir = {
        owner[c] for c in owner if c in new_graph and _dirname(new_graph[c].relative_path) == d
    }
    if len(same_dir) == 1:
        return RoutingDecision(cid, RULE_SAME_DIR, next(iter(same_dir)), detail=f"dir {d!r}")
    # Rule 3: neighbour majority.
    rev_users = {c for c, n in new_graph.items() if cid in (n.depends_on or ())}
    neighbours = {
        c for c in (set(node.depends_on or ()) | rev_users) if T.resolve_owner(owner, c) is not None
    }
    if neighbours:
        votes = Counter(T.resolve_owner(owner, c) for c in neighbours)
        leaf, n = votes.most_common(1)[0]
        share = n / len(neighbours)
        if share >= opts.tau_nb:
            return RoutingDecision(
                cid, RULE_NEIGHBOR, leaf, detail=f"{n}/{len(neighbours)} neighbours ({share:.2f})"
            )
    return None


def unique_leaf_name(tree: dict[str, Any], requested: str, parent: tuple[str, ...]) -> str:
    taken = collect_module_tree_names(tree)
    parent_name = parent[-1] if parent else None
    return resolve_unique_name(sanitize_module_name(requested), parent_name, taken)


def repair_tree(
    tree: dict[str, Any],
    diff: GraphDiff,
    new_graph: dict[str, Node],
    tracked_new: set[str],
    opts: UpdateOptions,
    route_orphans: OrphanRouter | None = None,
) -> RepairResult:
    """Return a repaired *copy* of ``tree`` plus every decision taken."""
    tree = T.copy_tree(tree)
    before = {p: set(T.components_of(i)) for p, i in T.iter_nodes(tree)}
    result = RepairResult(tree=tree)

    # Rename: rewrite ids in place.
    for old_id, new_id in diff.renamed.items():
        if T.rename_component(tree, old_id, new_id):
            result.renamed[old_id] = new_id

    # Delete: remove, then drop empty nodes.
    owner_before = T.owner_map(tree)
    for cid in sorted(diff.deleted):
        if cid in owner_before:
            T.remove_component(tree, cid)
            result.removed.append((cid, owner_before[cid]))
    result.deleted_nodes = T.prune_empty(tree)

    # Add: route every added component that the new build selected as tracked.
    owner = T.owner_map(tree)
    to_route = sorted(c for c in diff.added if c in tracked_new and c in new_graph)
    orphans: list[str] = []
    for cid in to_route:
        decision = _route_by_rules(cid, new_graph[cid], owner, new_graph, opts)
        if decision is None:
            orphans.append(cid)
            continue
        T.add_component(tree, decision.leaf_path, cid)
        owner[cid] = decision.leaf_path
        result.routing.append(decision)
    for cid in sorted(c for c in diff.added if c not in tracked_new):
        result.routing.append(
            RoutingDecision(cid, RULE_UNTRACKED, None, detail="not a selected leaf node")
        )

    # Rule 4: orphans.
    if orphans and route_orphans is not None and opts.use_routing_agent:
        context = {"tree": tree, "owner": owner, "graph": new_graph}
        decisions = route_orphans(orphans, context)
        decided = {d.component_id: d for d in decisions}
        for cid in orphans:
            d = decided.get(cid)
            if d is None or d.leaf_path is None:
                result.routing.append(
                    d or RoutingDecision(cid, RULE_AGENT, None, detail="agent left untracked")
                )
                result.orphans.append(cid)
                continue
            if d.new_leaf and T.node_at(tree, d.leaf_path) is None:
                parent, name = d.leaf_path[:-1], d.leaf_path[-1]
                if parent and T.node_at(tree, parent) is None:
                    result.routing.append(
                        RoutingDecision(cid, RULE_AGENT, None, detail=f"unknown parent {parent}")
                    )
                    result.orphans.append(cid)
                    continue
                name = unique_leaf_name(tree, name, parent)
                rel = new_graph[cid].relative_path
                new_path = T.insert_leaf(tree, parent, name, [], path_hint=_dirname(rel) or ".")
                d.leaf_path = new_path
                result.created_leaves.append(new_path)
            elif T.node_at(tree, d.leaf_path) is None:
                result.routing.append(
                    RoutingDecision(cid, RULE_AGENT, None, detail=f"unknown leaf {d.leaf_path}")
                )
                result.orphans.append(cid)
                continue
            T.add_component(tree, d.leaf_path, cid)
            owner[cid] = d.leaf_path
            d.rule = RULE_AGENT
            result.routing.append(d)
    else:
        for cid in orphans:
            result.routing.append(RoutingDecision(cid, RULE_AGENT, None, detail="no routing agent"))
        result.orphans.extend(orphans)

    # Tree deltas and growth check per unit.
    after = {p: set(T.components_of(i)) for p, i in T.iter_nodes(tree)}
    for path, now in after.items():
        was = before.get(path, set())
        entered = sorted(now - was)
        left = sorted(was - now)
        if entered:
            result.entered[path] = entered
        if left:
            result.left[path] = left
        if T.is_leaf(T.node_at(tree, path) or {}) and entered and now:
            g = len(set(entered)) / len(now)
            result.growth[path] = g
            if path not in result.created_leaves and g >= opts.tau_grow:
                result.growth_flagged.append(path)
    for path in before:
        if path not in after and before[path]:
            result.left[path] = sorted(before[path])

    logger.info(
        "Tree repair: %d renamed, %d removed, %d routed, %d orphans, %d nodes deleted, "
        "%d leaves created, %d growth-flagged",
        len(result.renamed),
        len(result.removed),
        sum(1 for r in result.routing if r.leaf_path is not None),
        len(result.orphans),
        len(result.deleted_nodes),
        len(result.created_leaves),
        len(result.growth_flagged),
    )
    return result
