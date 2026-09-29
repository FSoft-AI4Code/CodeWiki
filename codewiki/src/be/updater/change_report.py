"""Step 3: one change report per update unit (leaf), the active set, and the
fallback ratios of Step 4."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.graph_diff import GraphDiff
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.reference_index import unique_names_of
from codewiki.src.be.updater.tree_repair import RepairResult

logger = logging.getLogger(__name__)

MODE_EDIT = "edit"
MODE_CREATE = "create"
MODE_DELETE = "delete"


@dataclass
class LeafReport:
    leaf_path: tuple[str, ...]
    mode: str = MODE_EDIT
    own: list[str] = field(default_factory=list)
    # Own entries this leaf does not list: changed components no leaf tracks,
    # given to this leaf as effective owner (Step 3a). id -> placement rule.
    adopted: dict[str, str] = field(default_factory=dict)
    up: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)  # untracked changes next to this leaf
    refch: list[str] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    children_added: list[str] = field(default_factory=list)
    children_removed: list[str] = field(default_factory=list)
    reclustered: bool = False

    @property
    def page(self) -> str:
        return T.page_stem(self.leaf_path)

    @property
    def is_empty(self) -> bool:
        # ``context`` (untracked changes next to this leaf) is information for
        # an agent that runs anyway; on its own it never activates a leaf.
        return not (
            self.own
            or self.up
            or self.refch
            or self.entered
            or self.left
            or self.children_added
            or self.children_removed
            or self.reclustered
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["leaf_path"] = list(self.leaf_path)
        d["page"] = self.page
        return d


def _out_edges(cid: str, old: dict[str, Node], new: dict[str, Node]) -> set[str]:
    out: set[str] = set()
    if cid in old:
        out |= set(old[cid].depends_on or ())
    if cid in new:
        out |= set(new[cid].depends_on or ())
    return out


def _children_names(tree: dict[str, Any], path: tuple[str, ...]) -> set[str]:
    info = T.node_at(tree, path)
    if not info:
        return set()
    ch = info.get("children")
    return set(ch.keys()) if isinstance(ch, dict) else set()


def build_reports(
    diff: GraphDiff,
    old_tree: dict[str, Any],
    new_tree: dict[str, Any],
    old_graph: dict[str, Node],
    new_graph: dict[str, Node],
    ref_index: dict[str, dict[str, list[str]]] | None,
    repair: RepairResult,
    opts: UpdateOptions,
    reclustered: set[tuple[str, ...]] | None = None,
    adopted: dict[str, Any] | None = None,
) -> dict[tuple[str, ...], LeafReport]:
    """Build ``Report(l)`` for every unit of the new tree plus deleted leaves.

    ``adopted`` (Step 3a, ``ownership.close_ownership``) maps a changed
    component that no leaf tracks to its effective owner; it enters that
    leaf's Own exactly like a tracked component."""
    reclustered = reclustered or set()
    adopted = adopted or {}
    old_owner = T.owner_map(old_tree)
    new_owner = T.owner_map(new_tree)

    def owner_of(cid: str) -> tuple[str, ...] | None:
        o = T.resolve_owner(new_owner, cid) or T.resolve_owner(old_owner, cid)
        if o is None and cid in adopted:
            o = tuple(adopted[cid].leaf_path)
        return o

    old_units = set(T.unit_paths(old_tree))
    new_units = T.unit_paths(new_tree)
    reports: dict[tuple[str, ...], LeafReport] = {p: LeafReport(leaf_path=p) for p in new_units}

    # Own: changed components in comp_t(l) ∪ comp_{t+1}(l). An untracked
    # method counts as its class's (see tree.resolve_owner).
    for cid in diff.changed_ids | set(diff.deleted):
        owners = set()
        for o in (T.resolve_owner(new_owner, cid), T.resolve_owner(old_owner, cid)):
            if o is not None:
                owners.add(o)
        rec = diff.record_for(cid)
        if rec and rec.old_id:
            o = T.resolve_owner(old_owner, rec.old_id)
            if o is not None:
                owners.add(o)
        if not owners and cid in adopted:
            owners.add(tuple(adopted[cid].leaf_path))
        for p in owners:
            if p in reports and cid not in reports[p].own:
                reports[p].own.append(cid)
                if cid in adopted:
                    reports[p].adopted[cid] = adopted[cid].rule

    # Up: interface / deleted / renamed components used by this leaf's code.
    contract_moved = set(diff.interface) | set(diff.deleted) | set(diff.renamed.values())
    if opts.use_up and contract_moved:
        rev_old = T.reverse_edges(old_graph)
        rev_new = T.reverse_edges(new_graph)
        old_ids_of_renames = {v: k for k, v in diff.renamed.items()}
        for cid in contract_moved:
            frontier = {cid}
            if cid in old_ids_of_renames:
                frontier.add(old_ids_of_renames[cid])
            seen: set[str] = set()
            for _ in range(max(1, opts.k_hop)):
                nxt: set[str] = set()
                for x in frontier:
                    nxt |= rev_old.get(x, set()) | rev_new.get(x, set())
                nxt -= seen
                seen |= nxt
                frontier = nxt
            for user in seen:
                p = T.resolve_owner(new_owner, user) or T.resolve_owner(old_owner, user)
                if p is None or p not in reports:
                    continue
                own_leaf = owner_of(cid) or T.resolve_owner(
                    old_owner, old_ids_of_renames.get(cid, "")
                )
                if p == own_leaf:
                    continue
                if cid not in reports[p].up:
                    reports[p].up.append(cid)

    # Untracked changed components that the closure could not place either:
    # context for the leaves owning their neighbours.
    for cid in diff.changed_ids:
        if owner_of(cid) is not None:
            continue
        neighbours = _out_edges(cid, old_graph, new_graph)
        for other, node in new_graph.items():
            if cid in (node.depends_on or ()):
                neighbours.add(other)
        for other, node in old_graph.items():
            if cid in (node.depends_on or ()):
                neighbours.add(other)
        for n in neighbours:
            p = T.resolve_owner(new_owner, n) or T.resolve_owner(old_owner, n)
            if p in reports and cid not in reports[p].context:
                reports[p].context.append(cid)

    # RefCh: things this leaf's page refers to that changed or vanished.
    if ref_index:
        deleted_pages = {p[-1] for p in repair.deleted_nodes}
        # Bare-name mentions count only when the contract moved (interface,
        # delete, rename); a body-only change does not make a mention stale.
        gone_names = unique_names_of(old_graph, set(diff.deleted) | set(diff.renamed.keys()))
        gone_names |= unique_names_of(new_graph, set(diff.interface) | set(diff.renamed.values()))
        changed_ids = diff.changed_ids | set(diff.deleted) | set(diff.renamed.keys())
        for p, rep in reports.items():
            refs = ref_index.get(rep.page)
            if not refs:
                continue
            hits: list[str] = []
            hits += [x for x in refs.get("ids", []) if x in changed_ids]
            hits += [f"{x}.md" for x in refs.get("links", []) if x in deleted_pages]
            hits += [x for x in refs.get("names", []) if x in gone_names]
            own_set = set(rep.own)
            rep.refch = sorted({h for h in hits if h not in own_set})

    # Tree changes.
    for p, rep in reports.items():
        rep.entered = list(repair.entered.get(p, []))
        rep.left = list(repair.left.get(p, []))
        old_children = _children_names(old_tree, p)
        new_children = _children_names(new_tree, p)
        rep.children_added = sorted(new_children - old_children)
        rep.children_removed = sorted(old_children - new_children)
        rep.reclustered = p in reclustered
        if p in repair.created_leaves:
            rep.mode = MODE_CREATE
        elif p not in old_units and p not in reclustered:
            # A parent that became a unit (all children removed) or a brand new node.
            rep.mode = MODE_CREATE if T.node_at(old_tree, p) is None else MODE_EDIT

    # Deleted leaves: report with mode delete, keyed by the old path.
    for p in repair.deleted_nodes:
        if p in reports:
            continue
        rep = LeafReport(leaf_path=p, mode=MODE_DELETE)
        rep.left = list(repair.left.get(p, []))
        old_info = T.node_at(old_tree, p) or {}
        rep.own = [c for c in T.components_of(old_info) if c in diff.deleted]
        reports[p] = rep

    active = [p for p, r in reports.items() if not r.is_empty or r.mode != MODE_EDIT]
    logger.info("Change reports: %d units, %d active", len(reports), len(active))
    return reports


def active_set(reports: dict[tuple[str, ...], LeafReport]) -> list[tuple[str, ...]]:
    return [p for p, r in reports.items() if not r.is_empty or r.mode != MODE_EDIT]


def fallback_ratios(
    reports: dict[tuple[str, ...], LeafReport],
    new_tree: dict[str, Any],
    repair: RepairResult,
    reclustered: set[tuple[str, ...]] | None = None,
) -> dict[str, float]:
    n_leaves = max(1, len(T.unit_paths(new_tree)))
    active = active_set(reports)
    structural = len(repair.created_leaves) + len(repair.deleted_nodes) + len(reclustered or ())
    return {
        "r_leaf": len(active) / n_leaves,
        "r_tree": structural / n_leaves,
        "n_active": len(active),
        "n_leaves": n_leaves,
        "n_structural": structural,
    }


def order_active(
    active: list[tuple[str, ...]],
    new_tree: dict[str, Any],
    new_graph: dict[str, Node],
) -> list[tuple[str, ...]]:
    """Topological order under the lifted dependency: if A uses B, B runs first.
    Cycles and ties are broken by tree pre-order; deleted leaves (not in the
    new tree) go last."""
    pre = {p: i for i, p in enumerate(T.preorder_paths(new_tree))}
    dep = T.leaf_dependents(new_tree, new_graph)  # leaf -> set of leaves that use it
    active_set_ = set(active)
    # edges: used -> user (used first)
    indeg = {p: 0 for p in active}
    users_of: dict[tuple[str, ...], set[tuple[str, ...]]] = {p: set() for p in active}
    for used, users in dep.items():
        if used not in active_set_:
            continue
        for user in users:
            if user in active_set_ and user != used:
                users_of[used].add(user)
    for used, users in users_of.items():
        for user in users:
            indeg[user] += 1
    order: list[tuple[str, ...]] = []
    remaining = set(active)
    while remaining:
        ready = sorted((p for p in remaining if indeg[p] == 0), key=lambda p: pre.get(p, 10**9))
        if not ready:  # cycle: pick by pre-order
            ready = [min(remaining, key=lambda p: pre.get(p, 10**9))]
        p = ready[0]
        order.append(p)
        remaining.discard(p)
        for user in users_of.get(p, ()):
            if user in remaining:
                indeg[user] -= 1
    return order
