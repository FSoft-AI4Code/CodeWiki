"""Rule 4 of tree repair: the orphan routing agent (one single-shot call)."""

from __future__ import annotations

import logging
import time
from typing import Any

from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import pages as P
from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.prompts import ROUTING_SYSTEM_PROMPT, format_routing_prompt
from codewiki.src.be.updater.record import CallCost, UpdateRecord
from codewiki.src.be.updater.tree_repair import RULE_AGENT, RoutingDecision
from codewiki.src.be.updater.verdicts import parse_json_block

logger = logging.getLogger(__name__)


def _first_sentence(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("```") or s.startswith("|"):
            continue
        return s[:160]
    return ""


def tree_outline_with_summaries(tree: dict[str, Any], docs_dir: str) -> str:
    lines = []
    for path, info in T.iter_nodes(tree):
        name = path[-1]
        kind = "leaf" if T.is_leaf(info) else "parent"
        summary = _first_sentence(P.read_page(docs_dir, name))
        n = len(T.components_of(info))
        lines.append(f"{'  ' * (len(path) - 1)}- {name} [{kind}, {n} components] {summary}")
    return "\n".join(lines)


class RoutingAgent:
    def __init__(
        self, backend: LLMBackend, docs_dir: str, record: UpdateRecord, model: str | None = None
    ):
        self.backend = backend
        self.docs_dir = docs_dir
        self.record = record
        self.model = model

    def __call__(self, orphans: list[str], context: dict[str, Any]) -> list[RoutingDecision]:
        tree: dict[str, Any] = context["tree"]
        owner: dict[str, tuple[str, ...]] = context["owner"]
        graph: dict[str, Node] = context["graph"]
        # "ownership" (Step 3a): the components changed but no leaf lists them;
        # the agent names the page that answers for each one, or leaves it.
        # No new leaves, since the tree is not changed for that purpose.
        for_ownership = context.get("purpose") == "ownership"
        rev = T.reverse_edges(graph)
        neighbours = {}
        for cid in orphans:
            nb = set(graph[cid].depends_on or ()) | rev.get(cid, set())
            resolved = {T.resolve_owner(owner, c) for c in nb}
            neighbours[cid] = sorted({p[-1] for p in resolved if p is not None})
        prompt = (
            ROUTING_SYSTEM_PROMPT
            + "\n\n"
            + format_routing_prompt(
                tree_outline_with_summaries(tree, self.docs_dir),
                orphans,
                graph,
                neighbours,
                allow_create=not for_ownership,
            )
        )
        started = time.time()
        err = None
        text = ""
        try:
            text = self.backend.complete(prompt, model=self.model) or ""
        except Exception as e:  # noqa: BLE001 — recorded; orphans stay untracked
            err = f"{type(e).__name__}: {e}"
            logger.error("Routing agent failed: %s", e)
        self.record.add_call(
            CallCost(
                "routing",
                f"{len(orphans)} {'changed untracked' if for_ownership else 'orphans'}",
                time.time() - started,
                getattr(self.backend, "last_usage", None),
                err,
            )
        )
        data = parse_json_block(text) or {}
        by_name = {path[-1]: path for path, _ in T.iter_nodes(tree)}
        parents = {path[-1]: path for path, info in T.iter_nodes(tree) if not T.is_leaf(info)}
        decisions: list[RoutingDecision] = []
        for d in data.get("decisions", []) or []:
            if not isinstance(d, dict):
                continue
            cid = d.get("component_id")
            if cid not in orphans:
                continue
            action = str(d.get("action", "")).lower()
            reason = str(d.get("reason", ""))[:200]
            if action == "place" and d.get("leaf") in by_name:
                path = by_name[d["leaf"]]
                if not T.is_leaf(T.node_at(tree, path) or {}):
                    decisions.append(
                        RoutingDecision(cid, RULE_AGENT, None, detail=f"{d['leaf']} is not a leaf")
                    )
                    continue
                decisions.append(RoutingDecision(cid, RULE_AGENT, path, detail=reason))
            elif action == "create" and d.get("new_leaf") and for_ownership:
                decisions.append(
                    RoutingDecision(
                        cid, RULE_AGENT, None, detail="create not allowed for ownership; left"
                    )
                )
            elif action == "create" and d.get("new_leaf"):
                parent_name = d.get("parent")
                parent: tuple[str, ...] = ()
                if parent_name:
                    if parent_name in parents:
                        parent = parents[parent_name]
                    elif parent_name in by_name:
                        parent = by_name[parent_name][:-1]  # sibling of a leaf
                    else:
                        parent = ()
                decisions.append(
                    RoutingDecision(
                        cid,
                        RULE_AGENT,
                        parent + (str(d["new_leaf"]),),
                        new_leaf=True,
                        detail=reason,
                    )
                )
            else:
                decisions.append(
                    RoutingDecision(cid, RULE_AGENT, None, detail=reason or "untracked by agent")
                )
        return decisions
