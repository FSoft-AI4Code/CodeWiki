"""Checkpoint and retry regressions for batched module clustering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codewiki.src.be.cluster_batch_cache import (
    CACHE_VERSION,
    ClusterBatchCache,
    clustering_batch_cache_path,
    delete_clustering_batch_cache,
)
from codewiki.src.be.cluster_modules import cluster_modules
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config
from codewiki.src.utils import file_manager


def _tree(cid="a"):
    return {"Module": {"components": [cid]}}


def _inputs(tmp_path, directories=("alpha", "beta", "gamma")):
    ids = [f"{directory}/f{i}.py::C{i}" for directory in directories for i in range(2)]
    components = {}
    for cid in ids:
        path, name = cid.split("::")
        components[cid] = Node(
            id=cid,
            name=name,
            component_type="class",
            file_path=path,
            relative_path=path,
            source_code="code",
        )
    config = Config.from_cli(
        repo_path=str(tmp_path / "repo"),
        output_dir=str(tmp_path / "docs"),
        llm_base_url="http://localhost",
        llm_api_key="unused",
        main_model="m",
        cluster_model="m",
        min_token_per_module=1,
        max_token_per_module=2,
        max_leaf_nodes_per_cluster=2,
    )
    return ids, components, config


def _prompt_ids(prompt):
    section = prompt.split("<POTENTIAL_CORE_COMPONENTS>")[1].split("</POTENTIAL_CORE_COMPONENTS>")[
        0
    ]
    return [line.strip() for line in section.splitlines() if line.startswith("\t")]


def _response(name, ids):
    return f"<GROUPED_COMPONENTS>{ {name: {'components': ids}}!r}</GROUPED_COMPONENTS>"


def test_successful_batches_resume_after_failure(tmp_path):
    ids, components, config = _inputs(tmp_path)

    def first(prompt):
        batch = _prompt_ids(prompt)
        if batch[0].startswith("beta/"):
            raise RuntimeError("network failed")
        return _response("Alpha", batch)

    with pytest.raises(RuntimeError, match="network failed"):
        cluster_modules(ids, components, config, completer=first)
    calls = []

    def retry(prompt):
        batch = _prompt_ids(prompt)
        calls.append(batch)
        return _response(batch[0].split("/")[0].title(), batch)

    tree = cluster_modules(ids, components, config, completer=retry)
    assert calls == [ids[2:4], ids[4:6]]
    assert set(tree) == {"Alpha", "Beta", "Gamma"}


def test_interrupted_write_preserves_previous_checkpoint(tmp_path, monkeypatch):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree())

    def interrupted(data, stream, **kwargs):
        stream.write('{"version":')
        stream.flush()
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cache.store("__root__", 2, ["b"], _tree("b"))
    resumed = ClusterBatchCache(str(tmp_path))
    assert resumed.lookup("__root__", 1, ["a"]) == _tree()
    assert resumed.lookup("__root__", 2, ["b"]) is None
    assert sorted(p.name for p in (tmp_path / "temp").iterdir()) == ["clustering_batch_cache.json"]


def test_failed_replace_preserves_previous_checkpoint(tmp_path, monkeypatch, caplog):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree())

    def denied(*args):
        raise PermissionError("replace denied")

    monkeypatch.setattr("codewiki.src.be.cluster_batch_cache.os.replace", denied)
    cache.store("__root__", 2, ["b"], _tree("b"))
    resumed = ClusterBatchCache(str(tmp_path))
    assert resumed.lookup("__root__", 1, ["a"]) == _tree()
    assert resumed.lookup("__root__", 2, ["b"]) is None
    assert "replace denied" in caplog.text
    assert len(list((tmp_path / "temp").iterdir())) == 1


@pytest.mark.parametrize(
    "content",
    [
        "",
        '{"version":',
        "[]",
        "null",
        "42",
        '"text"',
        json.dumps({"version": -1, "entries": {}}),
        json.dumps({"version": CACHE_VERSION, "entries": []}),
    ],
)
def test_invalid_cache_is_a_miss(tmp_path, content):
    path = Path(clustering_batch_cache_path(str(tmp_path)))
    path.parent.mkdir()
    path.write_text(content)
    cache = ClusterBatchCache(str(tmp_path))
    assert cache.entry_count() == 0
    assert cache.lookup("__root__", 1, ["a"]) is None


def test_malformed_entries_are_ignored_individually(tmp_path):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree())
    path = Path(clustering_batch_cache_path(str(tmp_path)))
    data = json.loads(path.read_text())
    data["entries"].update({"null": None, "list": [], "bad": {"scope": "__root__"}})
    path.write_text(json.dumps(data))
    resumed = ClusterBatchCache(str(tmp_path))
    assert resumed.entry_count() == resumed.scope_entry_count("__root__") == 1
    assert resumed.lookup("__root__", 1, ["a"]) == _tree()


def test_unreadable_cache_is_a_miss(tmp_path, monkeypatch, caplog):
    def denied(path):
        raise PermissionError("read denied")

    monkeypatch.setattr(file_manager, "load_json", denied)
    assert ClusterBatchCache(str(tmp_path)).entry_count() == 0
    assert "read denied" in caplog.text


def test_cleanup_does_not_parse_cache(tmp_path):
    path = Path(clustering_batch_cache_path(str(tmp_path)))
    path.parent.mkdir()
    path.write_text("broken JSON")
    delete_clustering_batch_cache(str(tmp_path))
    delete_clustering_batch_cache(str(tmp_path))
    assert not path.exists()


def test_cache_write_failure_does_not_abort_clustering(tmp_path, monkeypatch):
    ids, components, config = _inputs(tmp_path)

    def denied(*args):
        raise PermissionError("cache unavailable")

    monkeypatch.setattr("codewiki.src.be.cluster_batch_cache.os.replace", denied)
    tree = cluster_modules(
        ids, components, config, completer=lambda p: _response(_prompt_ids(p)[0], _prompt_ids(p))
    )
    assert len(tree) == 3


def test_disabled_cache_never_writes(tmp_path):
    ids, components, config = _inputs(tmp_path)
    config.cluster_batch_cache = False
    cluster_modules(
        ids, components, config, completer=lambda p: _response(_prompt_ids(p)[0], _prompt_ids(p))
    )
    assert not Path(clustering_batch_cache_path(config.docs_dir)).exists()
