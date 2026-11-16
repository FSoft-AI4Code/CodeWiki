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
    clustering_request_fingerprint,
)
from codewiki.src.be.cluster_modules import cluster_modules, format_potential_core_components
from codewiki.src.be.prompt_template import format_cluster_prompt
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
    cache.store("__root__", 1, ["a"], _tree(), request_fingerprint="test-request")

    def interrupted(data, stream, **kwargs):
        stream.write('{"version":')
        stream.flush()
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cache.store("__root__", 2, ["b"], _tree("b"), request_fingerprint="test-request")
    resumed = ClusterBatchCache(str(tmp_path))
    assert resumed.lookup("__root__", 1, ["a"], request_fingerprint="test-request") == _tree()
    assert resumed.lookup("__root__", 2, ["b"], request_fingerprint="test-request") is None
    assert sorted(p.name for p in (tmp_path / "temp").iterdir()) == ["clustering_batch_cache.json"]


def test_failed_replace_preserves_previous_checkpoint(tmp_path, monkeypatch, caplog):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree(), request_fingerprint="test-request")

    def denied(*args):
        raise PermissionError("replace denied")

    monkeypatch.setattr("codewiki.src.be.cluster_batch_cache.os.replace", denied)
    cache.store("__root__", 2, ["b"], _tree("b"), request_fingerprint="test-request")
    resumed = ClusterBatchCache(str(tmp_path))
    assert resumed.lookup("__root__", 1, ["a"], request_fingerprint="test-request") == _tree()
    assert resumed.lookup("__root__", 2, ["b"], request_fingerprint="test-request") is None
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
    assert cache.lookup("__root__", 1, ["a"], request_fingerprint="test-request") is None


def test_malformed_entries_are_ignored_individually(tmp_path):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree(), request_fingerprint="test-request")
    path = Path(clustering_batch_cache_path(str(tmp_path)))
    data = json.loads(path.read_text())
    data["entries"].update({"null": None, "list": [], "bad": {"scope": "__root__"}})
    path.write_text(json.dumps(data))
    resumed = ClusterBatchCache(str(tmp_path))
    assert resumed.entry_count() == resumed.scope_entry_count("__root__") == 1
    assert resumed.lookup("__root__", 1, ["a"], request_fingerprint="test-request") == _tree()


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


def test_store_and_lookup_do_not_share_mutable_trees(tmp_path):
    cache = ClusterBatchCache(str(tmp_path))
    proposed = _tree()
    cache.store("__root__", 1, ["a"], proposed, request_fingerprint="test-request")
    proposed["Module"]["components"].append("outside")
    cached = cache.lookup("__root__", 1, ["a"], request_fingerprint="test-request")
    assert cached == _tree()
    cached["Module"]["components"].append("another")
    # A subsequent store persists the cache's own snapshot, not either mutated view.
    cache.store("__root__", 2, ["b"], _tree("b"), request_fingerprint="test-request")
    assert cache.lookup("__root__", 1, ["a"], request_fingerprint="test-request") == _tree()
    assert (
        ClusterBatchCache(str(tmp_path)).lookup(
            "__root__", 1, ["a"], request_fingerprint="test-request"
        )
        == _tree()
    )


def test_cached_components_must_belong_to_the_recorded_batch(tmp_path):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree(), request_fingerprint="test-request")
    path = Path(clustering_batch_cache_path(str(tmp_path)))
    data = json.loads(path.read_text())
    entry = next(iter(data["entries"].values()))
    entry["partial_tree"]["Module"]["components"].append("outside")
    path.write_text(json.dumps(data))
    assert (
        ClusterBatchCache(str(tmp_path)).lookup(
            "__root__", 1, ["a"], request_fingerprint="test-request"
        )
        is None
    )


def test_collisions_do_not_pollute_saved_batches_or_changed_input(tmp_path):
    ids, components, config = _inputs(tmp_path, ("alpha", "beta", "gamma", "omega"))

    def first(prompt):
        batch = _prompt_ids(prompt)
        if batch[0].startswith("omega/"):
            raise RuntimeError("network failed")
        name = "A" if batch[0].startswith(("alpha/", "beta/")) else "Gamma"
        return _response(name, batch)

    with pytest.raises(RuntimeError, match="network failed"):
        cluster_modules(ids, components, config, completer=first)
    cache = ClusterBatchCache(config.docs_dir)
    prompt = format_cluster_prompt(format_potential_core_components(ids[:2], components)[0])
    saved = cache.lookup(
        "__root__", 1, ids[:2], request_fingerprint=clustering_request_fingerprint(config, prompt)
    )
    assert saved["A"]["components"] == ids[:2]

    # Adding a file changes beta's batches, while alpha's cache key still matches.
    cid = "beta/f-1.py::New"
    ids.append(cid)
    components[cid] = Node(
        id=cid,
        name="New",
        component_type="class",
        file_path="beta/f-1.py",
        relative_path="beta/f-1.py",
        source_code="code",
    )

    def retry(prompt):
        batch = _prompt_ids(prompt)
        name = "A" if batch[0].startswith("alpha/") else "Z" + batch[0].split("/")[0]
        return _response(name, batch)

    resumed = cluster_modules(ids, components, config, completer=retry)
    config.cluster_batch_cache = False
    fresh = cluster_modules(ids, components, config, completer=retry)
    assert resumed["A"]["components"] == fresh["A"]["components"] == ids[:2]


def test_pre_fix_cache_versions_are_not_reused(tmp_path):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a"], _tree(), request_fingerprint="test-request")
    path = Path(clustering_batch_cache_path(str(tmp_path)))
    data = json.loads(path.read_text())
    data["version"] = CACHE_VERSION - 1
    path.write_text(json.dumps(data))
    assert (
        ClusterBatchCache(str(tmp_path)).lookup(
            "__root__", 1, ["a"], request_fingerprint="test-request"
        )
        is None
    )


@pytest.mark.parametrize(
    "failed_response",
    [
        None,
        "",
        "missing tags",
        "<GROUPED_COMPONENTS>{broken</GROUPED_COMPONENTS>",
        "<GROUPED_COMPONENTS>[]</GROUPED_COMPONENTS>",
        "<GROUPED_COMPONENTS>{'Unknown': {'components': ['missing::ID']}}</GROUPED_COMPONENTS>",
    ],
)
def test_retry_requeries_failed_responses_but_reuses_successes(tmp_path, failed_response):
    ids, components, config = _inputs(tmp_path)

    def first(prompt):
        batch = _prompt_ids(prompt)
        if batch[0].startswith("alpha/"):
            return failed_response
        if batch[0].startswith("gamma/"):
            raise RuntimeError("network failed")
        return _response("Beta", batch)

    with pytest.raises(RuntimeError, match="network failed"):
        cluster_modules(ids, components, config, completer=first)
    cache = ClusterBatchCache(config.docs_dir)
    assert cache.entry_count() == 1
    calls = []

    def retry(prompt):
        batch = _prompt_ids(prompt)
        calls.append(batch)
        return _response(batch[0].split("/")[0].title(), batch)

    tree = cluster_modules(ids, components, config, completer=retry)
    assert calls == [ids[:2], ids[4:6]]
    assert set(tree) == {"Alpha", "Beta", "Gamma"}


def test_structural_fallback_is_still_available_without_a_later_failure(tmp_path):
    ids, components, config = _inputs(tmp_path)
    tree = cluster_modules(ids, components, config, completer=lambda p: None)
    assert {cid for info in tree.values() for cid in info["components"]} == set(ids)
    assert not Path(clustering_batch_cache_path(config.docs_dir)).exists()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("cluster_model", "new-model"),
        ("main_model", "new-main"),
        ("provider", "codex"),
        ("llm_base_url", "http://another-provider"),
        ("azure_deployment", "new-deployment"),
        ("api_version", "new-version"),
        ("aws_region", "eu-west-1"),
        ("google_project", "new-project"),
        ("google_location", "us"),
        ("max_tokens", 64000),
        ("repo_path", "/another/repo"),
    ],
)
def test_changed_request_context_invalidates_successful_batches(tmp_path, attribute, value):
    ids, components, config = _inputs(tmp_path)

    def first(prompt):
        batch = _prompt_ids(prompt)
        if not batch[0].startswith("alpha/"):
            raise RuntimeError("network failed")
        return _response("OldAlpha", batch)

    with pytest.raises(RuntimeError, match="network failed"):
        cluster_modules(ids, components, config, completer=first)
    setattr(config, attribute, value)
    calls = []

    def retry(prompt):
        batch = _prompt_ids(prompt)
        calls.append(batch)
        return _response(batch[0].split("/")[0].title(), batch)

    tree = cluster_modules(ids, components, config, completer=retry)
    assert calls == [ids[:2], ids[2:4], ids[4:6]]
    assert "OldAlpha" not in tree


def test_main_model_change_invalidates_default_cluster_model(tmp_path):
    _, _, config = _inputs(tmp_path)
    config.provider = "codex"
    config.cluster_model = ""
    before = clustering_request_fingerprint(config, "same prompt")
    config.main_model = "luna"
    assert clustering_request_fingerprint(config, "same prompt") != before


def test_changed_recursive_prompt_is_not_reused(tmp_path):
    ids, components, config = _inputs(tmp_path)

    def outer(sibling):
        return {
            "Parent": {"components": ids, "children": {}},
            sibling: {"components": [], "children": {}},
        }

    def first(prompt):
        batch = _prompt_ids(prompt)
        if not batch[0].startswith("alpha/"):
            raise RuntimeError("network failed")
        return _response("OldAlpha", batch)

    with pytest.raises(RuntimeError, match="network failed"):
        cluster_modules(
            ids, components, config, outer("OldSibling"), "Parent", ["Parent"], completer=first
        )
    calls = []

    def retry(prompt):
        assert "NewSibling" in prompt
        batch = _prompt_ids(prompt)
        calls.append(batch)
        return _response(batch[0].split("/")[0].title(), batch)

    tree = cluster_modules(
        ids, components, config, outer("NewSibling"), "Parent", ["Parent"], completer=retry
    )
    assert calls == [ids[:2], ids[2:4], ids[4:6]]
    assert "OldAlpha" not in tree


def test_fingerprints_do_not_persist_prompts_or_credentials(tmp_path):
    _, _, config = _inputs(tmp_path)
    prompt = "raw-prompt-sentinel"
    config.llm_api_key = "private-key-sentinel"
    config.llm_base_url = "https://private-user:private-pass@example.invalid"
    fingerprint = clustering_request_fingerprint(config, prompt)
    cache = ClusterBatchCache(config.docs_dir)
    cache.store("__root__", 1, ["a"], _tree(), request_fingerprint=fingerprint)
    serialized = Path(clustering_batch_cache_path(config.docs_dir)).read_text()
    assert fingerprint in serialized
    for secret in (prompt, config.llm_api_key, config.llm_base_url, "private-pass"):
        assert secret not in serialized
    config.llm_api_key = "rotated-key"
    assert clustering_request_fingerprint(config, prompt) == fingerprint


def test_vertex_environment_routing_is_fingerprinted(tmp_path, monkeypatch):
    _, _, config = _inputs(tmp_path)
    config.provider = "google-vertex"
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "first-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    before = clustering_request_fingerprint(config, "prompt")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "second-project")
    assert clustering_request_fingerprint(config, "prompt") != before
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "first-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us")
    assert clustering_request_fingerprint(config, "prompt") != before


def test_request_fingerprint_and_component_set_must_match(tmp_path):
    cache = ClusterBatchCache(str(tmp_path))
    cache.store("__root__", 1, ["a", "b"], _tree(), request_fingerprint="request-one")
    assert cache.lookup("__root__", 1, ["b", "a"], request_fingerprint="request-one") == _tree()
    assert cache.lookup("__root__", 1, ["a", "b"], request_fingerprint="request-two") is None
    assert cache.lookup("__root__", 1, ["a", "c"], request_fingerprint="request-one") is None
