"""Step 3/4: per-leaf change reports, active set, fallback ratios, ordering."""

from __future__ import annotations

from updater_toy import (
    HANDLE,
    OAUTH,
    REFRESH,
    USER,
    VALIDATE,
    graph_r1,
    graph_r2,
    tracked_r2,
    tree_r1,
    write_pages,
)

from codewiki.src.be.updater.change_report import (
    MODE_DELETE,
    MODE_EDIT,
    active_set,
    build_reports,
    fallback_ratios,
    order_active,
)
from codewiki.src.be.updater.graph_diff import diff_graphs
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.reference_index import build_reference_index
from codewiki.src.be.updater.tree_repair import repair_tree


def _toy(tmp_path, opts=None):
    opts = opts or UpdateOptions()
    old_g, new_g = graph_r1(), graph_r2()
    old_t = tree_r1()
    d = diff_graphs(old_g, new_g, opts)
    r = repair_tree(old_t, d, new_g, tracked_r2(), opts)
    write_pages(tmp_path)
    idx = build_reference_index(str(tmp_path), old_g, old_t)
    reports = build_reports(d, old_t, r.tree, old_g, new_g, idx, r, opts)
    return d, r, reports, new_g


def test_toy_reports_match_part_7_table(tmp_path):
    d, r, reports, _ = _toy(tmp_path)
    auth = reports[("core", "auth")]
    assert set(auth.own) == {VALIDATE, REFRESH, OAUTH}
    assert auth.up == [] and auth.entered == [OAUTH] and auth.mode == MODE_EDIT
    api = reports[("core", "api")]
    assert api.own == [] and api.up == [REFRESH] and api.mode == MODE_EDIT
    # api's page names refresh by id and by name; its own Up already covers it,
    # but RefCh is about references, so it lists both forms.
    assert REFRESH in api.refch
    pipeline = reports[("pipeline",)]
    assert pipeline.own == [] and pipeline.up == []
    assert pipeline.refch == ["refresh"]  # names a component whose contract moved
    storage = reports[("storage",)]
    assert storage.mode == MODE_DELETE and storage.own == [USER]
    # parents have no report
    assert ("core",) not in reports
    active = active_set(reports)
    assert set(active) == {("core", "auth"), ("core", "api"), ("pipeline",), ("storage",)}


def test_body_only_change_does_not_reach_dependents(tmp_path):
    # Only validate's body changes: login (same leaf) uses it; api does not.
    old_g, new_g = graph_r1(), graph_r1()
    new_g[VALIDATE] = new_g[VALIDATE].model_copy(
        update={"source_code": "def validate(user, pw):\n    return True\n"}
    )
    opts = UpdateOptions()
    d = diff_graphs(old_g, new_g, opts)
    r = repair_tree(tree_r1(), d, new_g, tracked_r2() - {OAUTH}, opts)
    write_pages(tmp_path)
    idx = build_reference_index(str(tmp_path), old_g, tree_r1())
    reports = build_reports(d, tree_r1(), r.tree, old_g, new_g, idx, r, opts)
    assert active_set(reports) == [("core", "auth")]
    assert reports[("core", "auth")].own == [VALIDATE]
    assert reports[("core", "api")].is_empty


def test_up_disabled_at_rung_1(tmp_path):
    _, _, reports, _ = _toy(tmp_path, UpdateOptions.from_rung(1))
    assert reports[("core", "api")].up == []


def test_fallback_ratios_and_order(tmp_path):
    d, r, reports, new_g = _toy(tmp_path)
    ratios = fallback_ratios(reports, r.tree, r)
    assert ratios["n_leaves"] == 3 and ratios["n_active"] == 4
    assert ratios["r_leaf"] >= UpdateOptions().tau_full
    assert ratios["n_structural"] == 1  # storage deleted
    order = order_active(active_set(reports), r.tree, new_g)
    # api uses auth -> auth first; deleted storage (not in new tree) last
    assert order.index(("core", "auth")) < order.index(("core", "api"))
    assert order[-1] == ("storage",)


def test_whole_repo_mode_is_one_virtual_leaf(tmp_path):
    from codewiki.src.be.updater import tree as T

    old_g, new_g = graph_r1(), graph_r2()
    tracked_old = set(old_g)
    old_t = T.virtual_whole_repo_tree("repo", sorted(tracked_old))
    opts = UpdateOptions()
    d = diff_graphs(old_g, new_g, opts)
    r = repair_tree(old_t, d, new_g, tracked_r2(), opts)
    reports = build_reports(d, old_t, r.tree, old_g, new_g, None, r, opts)
    assert active_set(reports) == [("repo",)]
    assert set(reports[("repo",)].own) == {VALIDATE, REFRESH, OAUTH, USER}


def test_untracked_method_attaches_to_its_class_leaf(tmp_path):
    """Only classes are tracked in real trees; a method change must reach the
    class's leaf as Own, and a method signature change must reach the leaves
    whose methods call it as Up."""
    from updater_toy import node

    from codewiki.src.be.updater import tree as T

    store_cls = "src/db/store.py::Store"
    store_get = "src/db/store.py::Store.get"
    api_cls = "src/api/view.py::View"
    api_show = "src/api/view.py::View.show"
    old_g = {
        store_cls: node(store_cls, "class", "class Store:\n    pass\n"),
        store_get: node(
            store_get, "method", "def get(self, k):\n    return self.d[k]\n", ["self", "k"]
        ),
        api_cls: node(api_cls, "class", "class View:\n    pass\n"),
        api_show: node(
            api_show,
            "method",
            "def show(self):\n    return store.get(1)\n",
            ["self"],
            deps=[store_get],
        ),
    }
    new_g = {k: v.model_copy(deep=True) for k, v in old_g.items()}
    new_g[store_get] = node(
        store_get,
        "method",
        "def get(self, k, default=None):\n    return self.d.get(k, default)\n",
        ["self", "k", "default"],
    )
    tree = {
        "db": {"components": [store_cls], "children": {}},
        "api": {"components": [api_cls], "children": {}},
    }
    owner = T.owner_map(tree)
    assert T.resolve_owner(owner, store_get) == ("db",)
    assert T.resolve_owner(owner, "src/x.py::free_function") is None
    assert T.leaf_dependents(tree, new_g) == {("db",): {("api",)}, ("api",): set()}

    opts = UpdateOptions()
    d = diff_graphs(old_g, new_g, opts)
    assert d.interface == {store_get}
    r = repair_tree(tree, d, new_g, {store_cls, api_cls}, opts)
    reports = build_reports(d, tree, r.tree, old_g, new_g, None, r, opts)
    assert reports[("db",)].own == [store_get]
    assert reports[("api",)].up == [store_get]
    assert reports[("api",)].context == []


def _with_untracked_helper(helper: str, deps_of_handle=(REFRESH,), helper_deps=()):
    """Toy graphs where only an untracked free function ``helper`` changes (body only)."""
    from updater_toy import node

    old_g = graph_r1()
    old_g[helper] = node(helper, "function", "def helper():\n    return 1\n", deps=helper_deps)
    old_g[HANDLE] = old_g[HANDLE].model_copy(update={"depends_on": set(deps_of_handle)})
    new_g = {k: v.model_copy(deep=True) for k, v in old_g.items()}
    new_g[helper] = node(helper, "function", "def helper():\n    return 2\n", deps=helper_deps)
    return old_g, new_g


def _reports_with_closure(old_g, new_g, opts, router=None):
    from codewiki.src.be.updater.ownership import close_ownership

    d = diff_graphs(old_g, new_g, opts)
    r = repair_tree(tree_r1(), d, new_g, tracked_r2() - {OAUTH}, opts)
    adopted = close_ownership(d, tree_r1(), r.tree, old_g, new_g, opts, router, r)
    reports = build_reports(d, tree_r1(), r.tree, old_g, new_g, None, r, opts, adopted=adopted)
    return adopted, reports


def test_context_alone_does_not_activate(tmp_path):
    """Without the closure an untracked free function next to a leaf is context
    only. With it (default) rule 2, same directory with one leaf, adopts it into
    core/api's Own and the leaf is active."""
    helper = "src/api/util.py::helper"
    old_g, new_g = _with_untracked_helper(helper, deps_of_handle=(REFRESH, helper))

    off = UpdateOptions(use_ownership_closure=False)
    adopted, reports = _reports_with_closure(old_g, new_g, off)
    assert adopted == {}
    assert reports[("core", "api")].context == [helper]
    assert reports[("core", "api")].own == [] and active_set(reports) == []

    on = UpdateOptions()
    adopted, reports = _reports_with_closure(old_g, new_g, on)
    assert set(adopted) == {helper} and adopted[helper].rule == "2:same_dir"
    assert adopted[helper].leaf_path == ("core", "api") and adopted[helper].deleted is False
    rep = reports[("core", "api")]
    assert rep.own == [helper] and rep.adopted == {helper: "2:same_dir"}
    assert rep.context == []  # adopted ids are owned now, not context
    assert active_set(reports) == [("core", "api")]
    assert rep.to_dict()["adopted"] == {helper: "2:same_dir"}


def test_closure_rule_1_same_file():
    helper = "src/api/routes.py::helper"  # same file as HANDLE
    old_g, new_g = _with_untracked_helper(helper)
    adopted, reports = _reports_with_closure(old_g, new_g, UpdateOptions())
    assert adopted[helper].rule == "1:same_file" and adopted[helper].leaf_path == ("core", "api")
    assert reports[("core", "api")].own == [helper]


def test_closure_rule_3_neighbour_majority():
    helper = "src/misc/util.py::helper"  # fresh dir, only neighbour is HANDLE
    old_g, new_g = _with_untracked_helper(helper, deps_of_handle=(REFRESH, helper))
    adopted, reports = _reports_with_closure(old_g, new_g, UpdateOptions())
    assert adopted[helper].rule == "3:neighbor_majority"
    assert adopted[helper].leaf_path == ("core", "api")
    assert active_set(reports) == [("core", "api")]


def test_closure_unplaced_stays_context():
    """Fresh dir, no tracked neighbour, no routing agent: nothing adopts it and
    no leaf is active; the change is recorded nowhere but the diff."""
    helper = "src/misc/util.py::helper"
    old_g, new_g = _with_untracked_helper(helper)
    adopted, reports = _reports_with_closure(old_g, new_g, UpdateOptions())
    assert adopted == {} and active_set(reports) == []
    assert all(r.context == [] for r in reports.values())


def test_closure_rule_4_via_router():
    """The routing callable is asked for what rules 1-3 cannot place, with
    purpose=ownership; a placed decision adopts, create/untracked do not."""
    from codewiki.src.be.updater.tree_repair import RULE_AGENT, RoutingDecision

    helper = "src/misc/util.py::helper"
    other = "src/misc/other.py::other"
    old_g, new_g = _with_untracked_helper(helper)
    from updater_toy import node

    old_g[other] = node(other, "function", "def other():\n    return 1\n")
    new_g[other] = node(other, "function", "def other():\n    return 2\n")
    seen = {}

    def router(orphans, context):
        seen["orphans"] = list(orphans)
        seen["purpose"] = context.get("purpose")
        return [
            RoutingDecision(helper, RULE_AGENT, ("core", "auth"), detail="auth page covers it"),
            RoutingDecision(other, RULE_AGENT, ("brand", "new"), new_leaf=True),
        ]

    adopted, reports = _reports_with_closure(old_g, new_g, UpdateOptions(), router)
    assert set(seen["orphans"]) == {helper, other} and seen["purpose"] == "ownership"
    assert set(adopted) == {helper} and adopted[helper].rule == RULE_AGENT
    assert reports[("core", "auth")].own == [helper]
    assert reports[("core", "auth")].adopted == {helper: RULE_AGENT}
    assert active_set(reports) == [("core", "auth")]
    # rung 1 never asks
    adopted, reports = _reports_with_closure(old_g, new_g, UpdateOptions.from_rung(1), router)
    assert adopted == {} and active_set(reports) == []


def test_closure_deleted_untracked_uses_old_graph():
    from updater_toy import node

    helper = "src/api/routes.py::helper"
    old_g = graph_r1()
    old_g[helper] = node(helper, "function", "def helper():\n    return 1\n")
    new_g = graph_r1()
    adopted, reports = _reports_with_closure(old_g, new_g, UpdateOptions())
    assert adopted[helper].deleted is True and adopted[helper].rule == "1:same_file"
    assert reports[("core", "api")].own == [helper]
    assert adopted[helper].to_dict()["deleted"] is True
