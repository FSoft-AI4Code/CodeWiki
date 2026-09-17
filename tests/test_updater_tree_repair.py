"""Step 2 of the component-level updater: tree repair rules."""

from __future__ import annotations

from updater_toy import (
    HANDLE,
    OAUTH,
    PIPELINE,
    REFRESH,
    USER,
    graph_r1,
    graph_r2,
    node,
    tracked_r2,
    tree_r1,
)

from codewiki.src.be.updater import tree as T
from codewiki.src.be.updater.graph_diff import diff_graphs
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.tree_repair import (
    RULE_AGENT,
    RULE_NEIGHBOR,
    RULE_SAME_DIR,
    RULE_SAME_FILE,
    RoutingDecision,
    repair_tree,
)


def test_owner_map_uses_deepest_node_and_parents_keep_unions():
    owner = T.owner_map(tree_r1())
    assert owner[HANDLE] == ("core", "api")
    assert owner[USER] == ("storage",)
    assert T.unit_paths(tree_r1()) == [
        ("core", "auth"),
        ("core", "api"),
        ("storage",),
        ("pipeline",),
    ]


def test_toy_repair():
    old, new = graph_r1(), graph_r2()
    d = diff_graphs(old, new)
    r = repair_tree(tree_r1(), d, new, tracked_r2(), UpdateOptions())
    # storage lost its only component and is gone
    assert ("storage",) in r.deleted_nodes
    assert "storage" not in r.tree
    assert r.removed == [(USER, ("storage",))]
    # OAuthClient routed by rule 2 (same directory, one leaf) into auth and its ancestors
    dec = {x.component_id: x for x in r.routing}
    assert dec[OAUTH].rule == RULE_SAME_DIR and dec[OAUTH].leaf_path == ("core", "auth")
    assert OAUTH in r.tree["core"]["children"]["auth"]["components"]
    assert OAUTH in r.tree["core"]["components"]
    assert r.entered[("core", "auth")] == [OAUTH]
    # 1 of 4 is under the default third: not flagged
    assert r.growth[("core", "auth")] == 0.25
    assert r.growth_flagged == []
    # source tree untouched
    assert "storage" in tree_r1()


def test_rule_1_same_file_wins():
    tree = tree_r1()
    new = graph_r2()
    new["src/api/routes.py::other"] = node(
        "src/api/routes.py::other", "function", "def other(): pass"
    )
    d = diff_graphs(graph_r1(), new)
    r = repair_tree(tree, d, new, tracked_r2() | {"src/api/routes.py::other"}, UpdateOptions())
    dec = {x.component_id: x for x in r.routing}
    assert dec["src/api/routes.py::other"].rule == RULE_SAME_FILE
    assert dec["src/api/routes.py::other"].leaf_path == ("core", "api")


def test_rule_3_neighbor_majority_and_orphan():
    tree = tree_r1()
    new = graph_r2()
    nb = "src/util/helper.py::Helper"
    new[nb] = node(nb, "class", "class Helper: pass", deps=[REFRESH, HANDLE, PIPELINE])
    orphan = "src/misc/lonely.py::Lonely"
    new[orphan] = node(orphan, "class", "class Lonely: pass")
    d = diff_graphs(graph_r1(), new)
    tracked = tracked_r2() | {nb, orphan}
    # neighbours: refresh (auth), handle (api), Pipeline (pipeline) -> 1/3 each < 0.5 -> orphan
    r = repair_tree(tree, d, new, tracked, UpdateOptions(tau_nb=0.5, use_routing_agent=False))
    assert set(r.orphans) == {nb, orphan}
    r2 = repair_tree(tree, d, new, tracked, UpdateOptions(tau_nb=0.3, use_routing_agent=False))
    dec = {x.component_id: x for x in r2.routing}
    assert dec[nb].rule == RULE_NEIGHBOR
    assert r2.orphans == [orphan]


def test_routing_agent_can_create_a_leaf():
    tree = tree_r1()
    new = graph_r2()
    a, b = "src/misc/lonely.py::Lonely", "src/misc/lonely.py::Lonelier"
    new[a] = node(a, "class", "class Lonely: pass")
    new[b] = node(b, "class", "class Lonelier: pass")
    d = diff_graphs(graph_r1(), new)

    def router(orphans, ctx):
        return [
            RoutingDecision(
                cid, RULE_AGENT, ("core", "misc"), new_leaf=True, detail="new subsystem"
            )
            for cid in orphans
        ]

    r = repair_tree(tree, d, new, tracked_r2() | {a, b}, UpdateOptions(), route_orphans=router)
    assert r.created_leaves == [("core", "misc")]
    assert set(r.tree["core"]["children"]["misc"]["components"]) == {a, b}
    assert a in r.tree["core"]["components"]
    assert r.orphans == []
    assert ("core", "misc") not in r.growth_flagged  # created leaves are not growth-flagged


def test_untracked_added_components_are_not_routed():
    new = graph_r2()
    d = diff_graphs(graph_r1(), new)
    r = repair_tree(tree_r1(), d, new, tracked_r2() - {OAUTH}, UpdateOptions())
    dec = {x.component_id: x for x in r.routing}
    assert dec[OAUTH].leaf_path is None and dec[OAUTH].rule == "untracked"
    assert OAUTH not in T.tracked_ids(r.tree)


def test_rename_rewrites_all_levels_and_agent_inserted_nodes_survive():
    tree = tree_r1()
    tree["core"]["children"]["auth"]["children"] = {
        "auth_tokens": {"components": [REFRESH], "children": {}}  # agent-inserted: no path
    }
    old = graph_r1()
    new = graph_r2()
    moved = "src/auth/tokens.py::refresh"
    new[moved] = node(moved, "function", old[REFRESH].source_code, ["self"])  # pure move
    del new[REFRESH]
    d = diff_graphs(old, new, UpdateOptions(tau_ren=0.9))
    assert d.renamed == {REFRESH: moved}
    r = repair_tree(tree, d, new, (tracked_r2() - {REFRESH}) | {moved}, UpdateOptions())
    assert moved in r.tree["core"]["components"]
    assert moved in r.tree["core"]["children"]["auth"]["components"]
    assert r.tree["core"]["children"]["auth"]["children"]["auth_tokens"]["components"] == [moved]
    assert "path" not in r.tree["core"]["children"]["auth"]["children"]["auth_tokens"]


def test_prune_removes_parent_when_all_children_go():
    tree = {
        "p": {
            "components": ["a.py::A"],
            "children": {"c": {"components": ["a.py::A"], "children": {}}},
        }
    }
    T.remove_component(tree, "a.py::A")
    removed = T.prune_empty(tree)
    assert removed == [("p", "c"), ("p",)]
    assert tree == {}


def test_growth_flag():
    tree = tree_r1()
    new = graph_r2()
    extra = [f"src/api/more{i}.py::M{i}" for i in range(3)]
    for cid in extra:
        new[cid] = node(cid, "class", f"class {cid.split('::')[1]}: pass", deps=[HANDLE])
    d = diff_graphs(graph_r1(), new)
    r = repair_tree(tree, d, new, tracked_r2() | set(extra), UpdateOptions(tau_grow=0.33))
    assert ("core", "api") in r.growth_flagged
    assert r.growth[("core", "api")] == 0.75
