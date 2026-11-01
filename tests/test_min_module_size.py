"""Minimum sizes use full file scopes and are enforced before agents run."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.cluster_modules import (
    _structural_fallback,
    cluster_modules,
    ensure_artifact_module,
)
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.module_scope import (
    can_split_module,
    coalesce_small_modules,
    component_files,
    get_module_token_count,
    prepare_sized_submodule_partition,
    validate_module_file_coverage,
)
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import Config


def node(path, tokens):
    source = " x" * tokens
    assert count_tokens(source) == tokens
    cid = f"{path}::entry"
    return Node(
        id=cid,
        name="entry",
        component_type="function",
        file_path=path,
        relative_path=path,
        source_code=source,
    )


def graph(**sizes):
    return {n.id: n for path, tokens in sizes.items() for n in [node(path, tokens)]}


def config(tmp_path, **overrides):
    return Config.from_cli(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path),
        llm_base_url="http://localhost",
        llm_api_key="test",
        main_model="test",
        cluster_model="test",
        **overrides,
    )


def partition(components):
    return {cid.split("::")[0]: {"components": [cid], "children": {}} for cid in components}


@pytest.mark.parametrize(
    "tokens,depth,expected",
    [
        (1999, 1, False),
        (2000, 1, True),
        (3999, 2, False),
        (4000, 2, True),
        (12000, 3, False),
    ],
)
def test_shared_gate_respects_minimum_leaf_budget_and_depth(tmp_path, tokens, depth, expected):
    components = graph(**{"a.py": tokens // 2, "b.py": tokens - tokens // 2})
    cfg = config(tmp_path, max_depth=3, max_token_per_leaf_module=2000)
    assert can_split_module(components, list(components), cfg, depth) is expected
    one_file = {
        cid: n.model_copy(update={"relative_path": "one.py"}) for cid, n in components.items()
    }
    assert not can_split_module(one_file, list(one_file), cfg, depth)


@pytest.mark.parametrize("extension", ["c", "cpp", "cs", "go", "py", "ts"])
def test_size_includes_unlisted_methods_in_owned_files(tmp_path, extension):
    components = graph(**{f"a.{extension}": 10, f"b.{extension}": 1000})
    entry_ids = list(components)
    method = node(f"a.{extension}", 990).model_copy(update={"id": f"a.{extension}::method"})
    components[method.id] = method
    cfg = config(tmp_path, max_token_per_leaf_module=1)
    assert can_split_module(components, entry_ids, cfg, 1)
    specs, rejected = prepare_sized_submodule_partition(
        {"a": [entry_ids[0]], "b": [entry_ids[1]]},
        entry_ids,
        components,
        {},
        1000,
    )
    assert set(specs) == {"a", "b"} and rejected == {}


@pytest.mark.parametrize("small,expected_count", [(999, 2), (1000, 3)])
def test_coalesce_boundary_preserves_all_files_and_input(small, expected_count):
    components = graph(**{"src/a.c": small, "src/b.c": 1000, "other/c.c": 1000})
    original = partition(components)
    before = deepcopy(original)
    tree, rejected = coalesce_small_modules(original, components, 1000)
    assert len(tree) == expected_count
    assert original == before
    assert all(
        get_module_token_count(components, component_files(v["components"], components)) >= 1000
        for v in tree.values()
    )
    validate_module_file_coverage(tree, components)
    if small == 999:
        assert "999 code tokens below minimum 1000" in rejected["src/a.c"]
        assert "src/b.c" in rejected["src/a.c"]
    reverse, _ = coalesce_small_modules(dict(reversed(list(original.items()))), components, 1000)
    assert reverse == tree


def test_coalesce_prefers_fitting_sibling_and_reports_final_destination():
    components = graph(**{"src/tiny.c": 100, "src/huge.c": 4000, "other/room.c": 1000})
    tree, rejected = coalesce_small_modules(partition(components), components, 1000, 4000)
    assert "other/room.c" in rejected["src/tiny.c"]
    validate_module_file_coverage(tree, components)
    smalls = graph(**{"a.c": 100, "b.c": 200, "c.c": 800})
    tree, rejected = coalesce_small_modules(partition(smalls), smalls, 1000)
    assert set(tree) == {"c.c"}
    assert all("merged into 'c.c'" in reason for reason in rejected.values())


@pytest.mark.parametrize("mode", ["llm", "fallback"])
def test_scanner_header_does_not_create_an_identical_child(tmp_path, mode):
    components = graph(**{"src/scanners.c": 70645, "src/scanners.h": 879})
    cfg = config(tmp_path)
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return (
            f"<GROUPED_COMPONENTS>{partition(components)!r}</GROUPED_COMPONENTS>"
            if mode == "llm"
            else None
        )

    tree = cluster_modules(list(components), components, cfg, completer=complete)
    assert tree == {} and len(calls) == 1
    fallback = _structural_fallback(list(components), components, cfg.max_token_per_module)
    assert fallback == {}
    packed, _ = coalesce_small_modules(partition(components), components, 1000)
    assert len(packed) == 1
    validate_module_file_coverage(packed, components)


def test_clustering_coalesces_before_recursion_and_restores_coverage(tmp_path):
    components = graph(**{"src/a.c": 999, "src/b.c": 2000, "other/c.c": 2000})
    cfg = config(tmp_path, max_token_per_module=4000)
    calls = []

    def complete(prompt):
        calls.append(prompt)
        return f"<GROUPED_COMPONENTS>{partition(components)!r}</GROUPED_COMPONENTS>"

    tree = cluster_modules(list(components), components, cfg, completer=complete)
    assert len(tree) == 2 and len(calls) == 1
    assert all(not v["children"] for v in tree.values())
    validate_module_file_coverage(tree, components)


def test_all_small_clusters_can_combine_into_two_viable_modules(tmp_path):
    components = graph(**{f"src/{i}.c": 500 for i in range(4)})
    cfg = config(tmp_path, max_token_per_module=1000)
    tree = cluster_modules(
        list(components),
        components,
        cfg,
        completer=lambda p: f"<GROUPED_COMPONENTS>{partition(components)!r}</GROUPED_COMPONENTS>",
    )
    assert len(tree) == 2
    validate_module_file_coverage(tree, components)


def test_parent_too_small_for_two_children_skips_clustering(tmp_path):
    components = graph(**{"a.c": 700, "b.c": 700})
    calls = []
    assert (
        cluster_modules(
            list(components),
            components,
            config(tmp_path, max_token_per_module=1000),
            completer=lambda p: calls.append(p),
        )
        == {}
    )
    assert calls == []


@pytest.fixture(params=["pydantic", "caw"])
def delegate(request, tmp_path, monkeypatch):
    runs = []
    if request.param == "pydantic":
        from codewiki.src.be.agent_tools import generate_sub_module_documentations as mod

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

        async def write(agent, prompt, *, deps, **kwargs):
            runs.append((deps.current_module_name, list(deps.module_component_ids())))
            Path(deps.absolute_docs_path, f"{deps.current_module_name}.md").write_text(
                "# Module\n\nDocumentation."
            )

        monkeypatch.setattr(mod, "Agent", FakeAgent)
        monkeypatch.setattr(mod, "create_fallback_models", lambda c: None)
        monkeypatch.setattr(mod, "run_until_doc_written", write)

        def call(deps, specs):
            return asyncio.run(
                mod.generate_sub_module_documentation(SimpleNamespace(deps=deps), specs)
            )
    else:
        pytest.importorskip("caw")
        from codewiki.src.be.caw_toolkit import CawToolKit

        class Backend:
            def _run_module_agent_sync(self, **kwargs):
                runs.append((kwargs["module_name"], list(kwargs["core_component_ids"])))
                Path(tmp_path, f"{kwargs['module_name']}.md").write_text(
                    "# Module\n\nDocumentation."
                )

        def call(deps, specs):
            return CawToolKit(deps, Backend(), True)._run_sub_modules(specs)

    return call, runs


def deps_for(tmp_path, components):
    return CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components=components,
        path_to_current_module=[],
        current_module_name="repo",
        module_tree={},
        max_depth=3,
        current_depth=1,
        config=config(tmp_path, max_depth=3, max_token_per_leaf_module=4000),
        custom_instructions="",
    )


@pytest.mark.parametrize("kind", ["all", "almost", "single_file", "small_parent", "depth"])
def test_tools_reject_without_mutating_or_running_agents(tmp_path, delegate, kind):
    components = graph(**{"a.c": 5000, "b.c": 100})
    deps = deps_for(tmp_path, components)
    ids = list(components)
    specs = {"all": ids} if kind == "all" else {"large": ids[:1], "tiny": ids[1:]}
    if kind == "single_file":
        deps.components = {ids[0]: components[ids[0]]}
        specs = {"one": ids[:1]}
    elif kind == "small_parent":
        deps.components = graph(**{"a.c": 240, "b.c": 66})
    elif kind == "depth":
        deps.current_depth = deps.max_depth
    before = deepcopy(deps.module_tree)
    call, runs = delegate
    report = call(deps, specs)
    assert deps.module_tree == before and runs == []
    assert list(tmp_path.iterdir()) == []
    assert "Skipped" in report and "directly" in report
    assert deps.current_module_name == "repo" and deps.path_to_current_module == []


def test_tools_merge_tiny_child_into_nearby_sibling_and_restore_scope(tmp_path, delegate):
    components = graph(**{"src/tiny.c": 999, "src/near.c": 2000, "other/far.c": 2000})
    deps = deps_for(tmp_path, components)
    call, runs = delegate
    report = call(
        deps,
        {
            "tiny": ["src/tiny.c::entry"],
            "near": ["src/near.c::entry"],
            "far": ["other/far.c::entry"],
        },
    )
    assert {name for name, ids in runs} == {"near", "far"}
    assert not (tmp_path / "tiny.md").exists()
    assert "999 code tokens below minimum 1000" in report and "merged into 'near'" in report
    assert "link the existing pages" not in report
    assert deps.current_depth == 1 and deps.scope_component_ids is None
    validate_module_file_coverage(deps.module_tree, components)
    again = call(deps, {"tiny": list(components)})
    assert len(runs) == 2 and "no unassigned files" in again


def test_small_artifact_bundle_merges_into_leaf_and_updates_ancestors():
    components = graph(**{"src/core.c": 2000, "ci/task.yml": 50})
    artifact = components["ci/task.yml::entry"]
    artifact.component_type = "artifact"
    artifact.artifact_class = "ci"
    tree = {
        "Core": {
            "components": ["src/core.c::entry"],
            "children": {"Implementation": {"components": ["src/core.c::entry"], "children": {}}},
        }
    }
    result = ensure_artifact_module(tree, list(components), components)
    assert set(result) == {"Core"}
    assert artifact.id in result["Core"]["components"]
    assert artifact.id in result["Core"]["children"]["Implementation"]["components"]
    validate_module_file_coverage(result, components)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000"])
def test_invalid_minimum_is_rejected_by_config(tmp_path, value):
    from codewiki.cli.models.config import Configuration
    from codewiki.cli.utils.errors import ConfigurationError

    with pytest.raises(ValueError, match="positive integer"):
        config(tmp_path, min_token_per_module=value)
    with pytest.raises(ConfigurationError, match="positive integer"):
        Configuration.from_dict({"min_token_per_module": value})


def test_default_and_custom_minimum_reach_backend(tmp_path):
    from codewiki.cli.models.config import Configuration

    for data, expected in [({}, 1000), ({"min_token_per_module": 2000}, 2000)]:
        cfg = Configuration.from_dict(data)
        assert cfg.min_token_per_module == expected
        round_trip = Configuration.from_dict(cfg.to_dict())
        assert (
            round_trip.to_backend_config(".", str(tmp_path), "test").min_token_per_module
            == expected
        )


def test_cli_persists_and_shows_minimum(tmp_path, monkeypatch):
    import json
    import codewiki.cli.config_manager as cm
    from codewiki.cli.commands.config import config_set, config_show
    from codewiki.cli.commands.generate import generate_command

    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setenv("CODEWIKI_NO_KEYRING", "1")
    runner = CliRunner()
    result = runner.invoke(config_set, ["--min-token-per-module", "2000"])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "config.json").read_text())["min_token_per_module"] == 2000
    assert "2000" in runner.invoke(config_show).output
    for command in (config_set, generate_command):
        result = runner.invoke(command, ["--min-token-per-module", "0"])
        assert result.exit_code != 0 and "0" in result.output
        assert "--min-token-per-module" in runner.invoke(command, ["--help"]).output


def test_artifact_entry_points_follow_their_existing_file_owner():
    components = graph(**{"src/core.c": 2000, "src/other.c": 2000})
    artifact = node("src/core.c", 50).model_copy(
        update={
            "id": "src/core.c::artifact",
            "component_type": "artifact",
            "artifact_class": "config",
        }
    )
    components[artifact.id] = artifact
    tree = {
        "Core": {"components": ["src/core.c::entry"], "children": {}},
        "Other": {"components": ["src/other.c::entry"], "children": {}},
    }
    result = ensure_artifact_module(tree, list(components), components)
    assert set(result) == {"Core", "Other"}
    assert artifact.id in result["Core"]["components"]
    validate_module_file_coverage(result, components)


@pytest.mark.parametrize(
    "tokens,depth,expected",
    [
        (7999, 1, False),
        (8000, 1, True),
        (15999, 2, False),
        (16000, 2, True),
    ],
)
def test_default_delegation_gate_is_eight_thousand(tmp_path, tokens, depth, expected):
    from codewiki.cli.models.config import Configuration
    from codewiki.src.config import DEFAULT_MAX_TOKEN_PER_LEAF_MODULE

    cfg = config(tmp_path, max_depth=3)
    cli_cfg = Configuration.from_dict({})
    assert (
        cfg.max_token_per_leaf_module
        == cli_cfg.max_token_per_leaf_module
        == DEFAULT_MAX_TOKEN_PER_LEAF_MODULE
        == 8000
    )
    assert cli_cfg.to_backend_config(".", str(tmp_path), "test").max_token_per_leaf_module == 8000
    components = graph(**{"a.py": tokens // 2, "b.py": tokens - tokens // 2})
    assert can_split_module(components, list(components), cfg, depth) is expected
    # Existing explicit settings continue to override the new default.
    assert (
        Configuration.from_dict({"max_token_per_leaf_module": 4000}).max_token_per_leaf_module
        == 4000
    )
