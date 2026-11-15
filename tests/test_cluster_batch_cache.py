import json
import os
import tempfile

from codewiki.src.be.cluster_batch_cache import (
    ClusterBatchCache,
    batch_entry_key,
    clustering_batch_cache_path,
    clustering_cache_scope,
    delete_clustering_batch_cache,
)


def test_batch_entry_key_stable_for_same_nodes():
    nodes = ["b.py::B", "a.py::A"]
    k1 = batch_entry_key("__root__", 3, nodes)
    k2 = batch_entry_key("__root__", 3, list(reversed(nodes)))
    assert k1 == k2
    assert k1.startswith("__root__|3|")


def test_batch_entry_key_differs_when_nodes_change():
    k1 = batch_entry_key("__root__", 3, ["a.py::A"])
    k2 = batch_entry_key("__root__", 3, ["a.py::A", "b.py::B"])
    assert k1 != k2


def test_clustering_cache_scope():
    assert clustering_cache_scope([]) == "__root__"
    assert clustering_cache_scope(["auth", "login"]) == "auth/login"


def test_store_and_lookup_round_trip():
    with tempfile.TemporaryDirectory() as docs_dir:
        cache = ClusterBatchCache(docs_dir)
        partial = {"module_a": {"path": "src", "components": ["x.py::X"]}}
        nodes = ["x.py::X", "y.py::Y"]
        cache.store("__root__", 1, nodes, partial)
        assert cache.lookup("__root__", 1, nodes) == partial
        assert cache.lookup("__root__", 2, nodes) is None
        path = clustering_batch_cache_path(docs_dir)
        assert os.path.isfile(path)
        on_disk = json.loads(open(path, encoding="utf-8").read())
        assert on_disk["version"] == 1
        assert len(on_disk["entries"]) == 1


def test_lookup_rejects_mismatched_node_ids():
    with tempfile.TemporaryDirectory() as docs_dir:
        cache = ClusterBatchCache(docs_dir)
        partial = {"m": {"path": "", "components": ["a.py::A"]}}
        cache.store("__root__", 5, ["a.py::A"], partial)
        assert cache.lookup("__root__", 5, ["a.py::A", "b.py::B"]) is None


def test_delete_clustering_batch_cache():
    with tempfile.TemporaryDirectory() as docs_dir:
        cache = ClusterBatchCache(docs_dir)
        cache.store("__root__", 1, ["a.py::A"], {"m": {"path": "", "components": ["a.py::A"]}})
        path = clustering_batch_cache_path(docs_dir)
        assert os.path.isfile(path)
        delete_clustering_batch_cache(docs_dir)
        assert not os.path.isfile(path)
        delete_clustering_batch_cache(docs_dir)