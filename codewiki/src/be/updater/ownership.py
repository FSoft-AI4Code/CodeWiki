"""Step 3a: ownership closure.

The module tree tracks a subset of the code graph (on svelte 764 of 2239
components). A changed component outside that subset has no owner, so it
can never enter a leaf's Own and, when its neighbours' leaves are otherwise
quiet, is dropped. Here the owner map is extended to a total function: an
untracked changed component gets an *effective owner* by the very rules that
place a new component in Step 2 (same file, same directory with one leaf,
neighbour majority, routing agent). The component then enters Own of that
leaf. Activation only: the tree on disk is not changed, and the closure is
recomputed at every step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.graph_diff import GraphDiff
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.tree_repair import (
    RULE_AGENT,
    OrphanRouter,
    RepairResult,
    RoutingDecision,
    route_by_rules,
)

logger = logging.getLogger(__name__)

PURPOSE_OWNERSHIP = "ownership"


@dataclass
class Adoption:
    """Effective owner of one changed component that no leaf tracks."""

    decision: RoutingDecision
    deleted: bool = False  # the component exists only in the old graph

    @property
    def component_id(self) -> str:
        return self.decision.component_id

    @property
    def leaf_path(self) -> tuple[str, ...]:
        assert self.decision.leaf_path is not None
        return self.decision.leaf_path

    @property
    def rule(self) -> str:
        return self.decision.rule

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "rule": self.rule,
            "leaf_path": list(self.leaf_path),
            "detail": self.decision.detail,
            "deleted": self.deleted,
        }


def untracked_changed(
    diff: GraphDiff,
    old_owner: dict[str, tuple[str, ...]],
    new_owner: dict[str, tuple[str, ...]],
    repair: RepairResult | None,
) -> list[str]:
    """Changed or deleted ids with no (class-resolved) owner in either tree,
    minus added components the repair step deliberately left untracked."""
    left_out = set()
    if repair is not None:
        left_out = {d.component_id for d in repair.routing if d.leaf_path is None}
    out = []
    for cid in sorted(diff.changed_ids | set(diff.deleted)):
        if cid in left_out:
            continue
        if T.resolve_owner(new_owner, cid) or T.resolve_owner(old_owner, cid):
            continue
        out.append(cid)
    return out


def close_ownership(
    diff: GraphDiff,
    old_tree: dict[str, Any],
    new_tree: dict[str, Any],
    old_graph: dict[str, Node],
    new_graph: dict[str, Node],
    opts: UpdateOptions,
    route_orphans: OrphanRouter | None = None,
    repair: RepairResult | None = None,
) -> dict[str, Adoption]:
    """Return ``{component_id: Adoption}`` for every changed untracked component
    that rules 1-3, or the routing agent as rule 4, can give an owner."""
    if not opts.use_ownership_closure:
        return {}
    old_owner = T.owner_map(old_tree)
    new_owner = T.owner_map(new_tree)
    candidates = untracked_changed(diff, old_owner, new_owner, repair)
    if not candidates:
        return {}

    adopted: dict[str, Adoption] = {}
    unplaced: list[str] = []
    for cid in candidates:
        deleted = cid not in new_graph and cid in old_graph
        graph, owner = (old_graph, old_owner) if deleted else (new_graph, new_owner)
        node = graph.get(cid)
        if node is None:
            continue
        decision = route_by_rules(cid, node, owner, graph, opts)
        if decision is None:
            if deleted:
                continue  # no page can answer for a vanished untracked component
            unplaced.append(cid)
            continue
        adopted[cid] = Adoption(decision, deleted)

    if unplaced and route_orphans is not None and opts.use_routing_agent:
        context = {
            "tree": new_tree,
            "owner": new_owner,
            "graph": new_graph,
            "purpose": PURPOSE_OWNERSHIP,
        }
        for d in route_orphans(unplaced, context):
            if d.component_id not in unplaced or d.component_id in adopted:
                continue
            if d.leaf_path is None or d.new_leaf or T.node_at(new_tree, d.leaf_path) is None:
                continue  # left untracked, or a new leaf: the tree is not changed here
            d.rule = RULE_AGENT
            adopted[d.component_id] = Adoption(d)

    by_rule: dict[str, int] = {}
    for a in adopted.values():
        by_rule[a.rule] = by_rule.get(a.rule, 0) + 1
    logger.info(
        "Ownership closure: %d untracked changed, %d adopted %s, %d left as context",
        len(candidates),
        len(adopted),
        by_rule,
        len(candidates) - len(adopted),
    )
    return adopted
