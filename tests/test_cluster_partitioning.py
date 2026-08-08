"""Tests for structure-based clustering batches and None-robust LLM handling.

Covers partition_leaf_nodes_by_structure (directory splitting, chunking of
unsplittable groups, coalescing, determinism), the batched cluster_modules
flow (flat merge, name-collision union, per-batch fallback), and the guards
that turn an empty/None LLM response into a graceful fallback instead of a
TypeError (issue seen on the electron run: output truncated at max_tokens,
provider returned content: null).
"""

from __future__ import annotations

from types import SimpleNamespace

from codewiki.src.be.cluster_modules import (
    _batch_fallback_name,
    _cluster_batch_fits,
    _merge_module_trees,
    _parse_super_group_response,
    cluster_modules,
    partition_leaf_nodes_by_structure,
    super_group_modules,
)
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import _extract_content
from codewiki.src.config import Config


def _make_node(component_id: str, source_code: str = "code") -> Node:
    rel_path, _, name = component_id.partition("::")
    return Node(
        id=component_id,
        name=name or component_id,
        component_type="class",
        file_path=rel_path,
        relative_path=rel_path,
        source_code=source_code,
    )


def _components(component_ids: list[str]) -> dict[str, Node]:
    return {cid: _make_node(cid) for cid in component_ids}


def _config(**overrides) -> Config:
    kwargs = dict(
        repo_path="/tmp/repo",
        output_dir="/tmp/out",
        llm_base_url="http://localhost",
        llm_api_key="key",
        main_model="m",
        cluster_model="m",
    )
    return Config.from_cli(**kwargs, **overrides)


def _max_count_fits(limit: int):
    """A fits() predicate on node count only — keeps tests tokenizer-free."""
    return lambda ids: len(ids) <= limit


# ---------------------------------------------------------------------------
# partition_leaf_nodes_by_structure
# ---------------------------------------------------------------------------

def test_partition_single_batch_when_everything_fits():
    ids = [f"lib/a.py::C{i}" for i in range(5)]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(10))
    assert batches == [sorted(ids)]


def test_partition_drops_unknown_ids():
    ids = ["lib/a.py::A", "lib/b.py::B"]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(
        ids + ["gone/x.py::X"], components, _max_count_fits(10)
    )
    assert batches == [ids]


def test_partition_splits_by_top_level_directory():
    ids = [f"alpha/f{i}.py::A{i}" for i in range(4)] + [
        f"beta/f{i}.py::B{i}" for i in range(4)
    ]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(4))
    assert len(batches) == 2
    assert all(len(batch) == 4 for batch in batches)
    # Path-sorted: alpha before beta.
    assert all(cid.startswith("alpha/") for cid in batches[0])
    assert all(cid.startswith("beta/") for cid in batches[1])


def test_partition_recurses_into_oversized_directory():
    ids = [f"pkg/sub{i // 3}/f{i}.py::C{i}" for i in range(9)]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(3))
    assert all(len(batch) <= 3 for batch in batches)
    union = sorted(cid for batch in batches for cid in batch)
    assert union == sorted(ids)


def test_partition_chunks_unsplittable_single_file():
    ids = [f"one/file.py::C{i:02d}" for i in range(10)]
    components = _components(ids)
    batches = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(4))
    assert all(len(batch) <= 4 for batch in batches)
    union = [cid for batch in batches for cid in batch]
    assert sorted(union) == sorted(ids)


def test_partition_coalesces_root_files_and_is_deterministic():
    ids = (
        ["setup.py::Setup", "conftest.py::Conf"]
        + [f"src/f{i}.py::S{i}" for i in range(3)]
    )
    components = _components(ids)
    first = partition_leaf_nodes_by_structure(ids, components, _max_count_fits(4))
    second = partition_leaf_nodes_by_structure(
        list(reversed(ids)), components, _max_count_fits(4)
    )
    assert first == second
    assert all(len(batch) <= 4 for batch in first)
    # Root-level files must not sit alone in single-node batches.
    assert all(len(batch) > 1 for batch in first)
    union = sorted(cid for batch in first for cid in batch)
    assert union == sorted(ids)


def test_cluster_batch_fits_respects_node_count_and_token_budget():
    config = _config(max_leaf_nodes_per_cluster=2, max_tokens=64000)
    assert _cluster_batch_fits(["a/b.py::X"], config)
    assert not _cluster_batch_fits(["a/b.py::X", "a/c.py::Y", "a/d.py::Z"], config)
    # Tiny max_tokens shrinks the output budget below even a few IDs.
    tight = _config(max_leaf_nodes_per_cluster=600, max_tokens=1)
    long_ids = ["dir/" + "verylongsegment" * 400 + f".py::C{i}" for i in range(3)]
    assert not _cluster_batch_fits(long_ids, tight)


# ---------------------------------------------------------------------------
# None-robustness
# ---------------------------------------------------------------------------

def test_cluster_modules_none_response_returns_empty_tree():
    ids = [f"lib/f{i}.py::C{i}" for i in range(3)]
    components = _components(ids)
    config = _config(max_token_per_module=1)
    result = cluster_modules(
        ids, components, config, {}, None, [], completer=lambda prompt: None
    )
    assert result == {}


def test_parse_super_group_response_handles_none_and_garbage():
    assert _parse_super_group_response(None) is None
    assert _parse_super_group_response("") is None
    assert _parse_super_group_response("no tags here") is None


def test_super_group_modules_none_response_keeps_tree():
    tree = {
        f"Module{i}": {"path": f"m{i}", "components": [f"m{i}/f.py::C"]}
        for i in range(5)
    }
    config = _config()
    assert super_group_modules(tree, config, completer=lambda prompt: None) == tree


def test_extract_content_returns_none_on_truncated_empty_response():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None), finish_reason="length"
            )
        ]
    )
    assert _extract_content(response, "test-model") is None
    ok = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="hi"), finish_reason="stop")
        ]
    )
    assert _extract_content(ok, "test-model") == "hi"


# ---------------------------------------------------------------------------
# Batched clustering end-to-end (scripted completer)
# ---------------------------------------------------------------------------

def _grouped(tree: dict) -> str:
    return f"<GROUPED_COMPONENTS>{tree!r}</GROUPED_COMPONENTS>"


def test_batched_clustering_merges_batches_and_collisions():
    alpha = [f"alpha/f{i}.py::A{i}" for i in range(3)]
    beta = [f"beta/f{i}.py::B{i}" for i in range(3)]
    components = _components(alpha + beta)
    config = _config(max_token_per_module=1, max_leaf_nodes_per_cluster=3)

    def completer(prompt: str) -> str:
        if "<MODULE_TREE>" in prompt:
            return None  # recursive module-level calls: decline so recursion stops
        in_alpha = alpha[0] in prompt
        in_beta = beta[0] in prompt
        if in_alpha and not in_beta:
            return _grouped({
                "Alpha": {"path": "alpha", "components": alpha[:2]},
                "Shared": {"path": "alpha", "components": [alpha[2]]},
            })
        if in_beta and not in_alpha:
            return _grouped({
                "Beta": {"path": "beta", "components": beta[:2]},
                "Shared": {"path": "beta", "components": [beta[2]]},
            })
        return None

    tree = cluster_modules(
        alpha + beta, components, config, {}, None, [], completer=completer
    )
    assert set(tree) == {"Alpha", "Shared", "Beta"}
    # Name collision across batches merges components and generalizes the path.
    assert tree["Shared"]["components"] == [alpha[2], beta[2]]
    assert tree["Shared"]["path"] == ""
    assert tree["Alpha"]["components"] == alpha[:2]


def test_batched_clustering_failed_batch_becomes_fallback_module():
    alpha = [f"alpha/f{i}.py::A{i}" for i in range(3)]
    beta = [f"beta/f{i}.py::B{i}" for i in range(3)]
    components = _components(alpha + beta)
    config = _config(max_token_per_module=1, max_leaf_nodes_per_cluster=3)

    def completer(prompt: str) -> str:
        if "<MODULE_TREE>" in prompt:
            return None  # recursive module-level calls: decline so recursion stops
        if beta[0] in prompt and alpha[0] not in prompt:
            return None  # this batch's call "truncates"
        if alpha[0] in prompt and beta[0] not in prompt:
            return _grouped({
                "Alpha": {"path": "alpha", "components": alpha[:2]},
                "AlphaExtra": {"path": "alpha", "components": [alpha[2]]},
            })
        return None

    tree = cluster_modules(
        alpha + beta, components, config, {}, None, [], completer=completer
    )
    assert "Alpha" in tree and "AlphaExtra" in tree
    fallback = tree["beta"]
    assert fallback["components"] == beta
    assert fallback["path"] == "beta"


def test_merge_module_trees_and_fallback_name_helpers():
    target = {"M": {"path": "a/b", "components": ["a/b/x.py::X"]}}
    _merge_module_trees(
        target, {"M": {"path": "a/c", "components": ["a/c/y.py::Y", "a/b/x.py::X"]}}
    )
    assert target["M"]["components"] == ["a/b/x.py::X", "a/c/y.py::Y"]
    assert target["M"]["path"] == "a"

    ids = ["pkg/mod/f.py::A", "pkg/mod/g.py::B"]
    components = _components(ids)
    assert _batch_fallback_name(ids, components, {}) == "pkg_mod"
    assert _batch_fallback_name(ids, components, {"pkg_mod": {}}) == "pkg_mod_2"
