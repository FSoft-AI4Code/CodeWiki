"""Previous-graph lookup when the repo was analysed from differently named checkouts."""

from __future__ import annotations

import json
import os
import time

from codewiki.src.be.updater.graph_store import (
    find_any_graph_file,
    graph_file_path,
    prune_superseded_graphs,
    snapshot_old_graph,
)


def _write_graph(d, name, payload):
    path = os.path.join(d, f"{name}_dependency_graph.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def test_single_candidate_is_used_whatever_its_name(tmp_path):
    d = str(tmp_path)
    old = _write_graph(d, "rq3_svelte_anchor", {"a": 1})
    assert find_any_graph_file(d) == old
    prev = snapshot_old_graph(d, "/repos/rq3-svelte-w1")
    assert prev is not None and prev.endswith("rq3_svelte_w1_dependency_graph.prev.json")
    assert json.load(open(prev)) == {"a": 1}


def test_several_candidates_pick_newest_not_none(tmp_path):
    d = str(tmp_path)
    older = _write_graph(d, "rq3_svelte_anchor", {"rev": "anchor"})
    time.sleep(0.01)
    newer = _write_graph(d, "rq3_svelte_w1", {"rev": "w1"})
    os.utime(older, (1, 1))  # make the order unambiguous
    assert find_any_graph_file(d) == newer
    prev = snapshot_old_graph(d, "/repos/rq3-svelte-w2")
    assert json.load(open(prev)) == {"rev": "w1"}


def test_prev_snapshots_are_not_candidates(tmp_path):
    d = str(tmp_path)
    _write_graph(d, "rq3_svelte_w1", {"rev": "w1"})
    with open(os.path.join(d, "rq3_svelte_w1_dependency_graph.prev.json"), "w") as f:
        json.dump({"rev": "anchor"}, f)
    assert find_any_graph_file(d).endswith("rq3_svelte_w1_dependency_graph.json")


def test_prune_keeps_current_and_prev_only(tmp_path):
    d = str(tmp_path)
    _write_graph(d, "rq3_svelte_anchor", {})
    _write_graph(d, "rq3_svelte_w1", {})
    current = _write_graph(d, "rq3_svelte_w2", {})
    with open(os.path.join(d, "rq3_svelte_w2_dependency_graph.prev.json"), "w") as f:
        json.dump({}, f)
    removed = prune_superseded_graphs(d, keep=graph_file_path(d, "/repos/rq3-svelte-w2"))
    assert sorted(os.path.basename(r) for r in removed) == [
        "rq3_svelte_anchor_dependency_graph.json",
        "rq3_svelte_w1_dependency_graph.json",
    ]
    assert sorted(os.listdir(d)) == [
        "rq3_svelte_w2_dependency_graph.json",
        "rq3_svelte_w2_dependency_graph.prev.json",
    ]
    assert os.path.exists(current)
