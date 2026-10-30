"""Regression tests for sizes and complete, disjoint source-file ownership."""

from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codewiki.src.be.cluster_modules import cluster_modules, get_clustering_input_token_count
from codewiki.src.be.dependency_analyzer.dependency_graphs_builder import DependencyGraphBuilder
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.module_scope import (
    complete_file_candidates,
    component_files,
    get_module_token_count,
    module_file_owners,
    normalize_module_partition,
    prepare_submodule_partition,
    validate_module_file_coverage,
)
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import Config


def _node(cid: str, source: str = "code", kind: str = "method") -> Node:
    path, name = cid.split("::")
    return Node(
        id=cid,
        name=name,
        component_type=kind,
        file_path=path,
        relative_path=path,
        source_code=source,
    )


def _config(repo: Path, out: Path, threshold: int = 36369) -> Config:
    return Config.from_cli(
        repo_path=str(repo),
        output_dir=str(out),
        llm_base_url="http://localhost",
        llm_api_key="test",
        main_model="test",
        cluster_model="test",
        max_token_per_module=threshold,
        artifacts_enabled=False,
    )


def test_go_method_heavy_package_splits_with_sparse_leaves(tmp_path, caplog):
    caplog.set_level("INFO")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.org/engine\n\ngo 1.22\n")
    for batch in range(3):
        types = ["package engine\n"]
        methods = ["package engine\n"]
        for i in range(batch * 135, (batch + 1) * 135):
            field = f"previous *S{i - 1}" if i else "value int"
            types.append(f"type S{i} struct {{ {field} }}\n")
            for name in ("Step", "Reset"):
                methods.append(
                    f"func (s *S{i}) {name}() int {{ value := {i}; "
                    "for n := 0; n < 8; n++ { value += n }; return value }\n"
                )
        (repo / f"types_{batch}.go").write_text("".join(types))
        (repo / f"methods_{batch}.go").write_text("".join(methods))
    (repo / "entry.go").write_text("package engine\ntype Entry struct {}\n")
    (repo / "constants.go").write_text("package engine\nconst Capacity = 4096\n")
    (repo / "ignored_test.go").write_text("package engine\ntype TestOnly struct {}\n")
    config = _config(repo, tmp_path / "out", 4000)
    components, leaves = DependencyGraphBuilder(config).build_dependency_graph()
    files = component_files(list(components), components)
    assert len(leaves) < 10
    assert len(component_files(leaves, components)) / len(files) < 0.5
    assert get_clustering_input_token_count(leaves, components) < config.max_token_per_module
    assert get_module_token_count(components) > config.max_token_per_module
    assert components["constants.go::__file__"].component_type == "file"
    assert "ignored_test.go" not in files
    candidates = complete_file_candidates(leaves, components)
    calls = []

    def completer(prompt):
        calls.append(prompt)
        # Include omissions, a duplicate file assignment, an invalid ID, and
        # a method which was not a leaf. Reconciliation must retain all files.
        first = candidates[0]
        other = next(cid for cid in candidates if components[cid].component_type == "method")
        return f'<GROUPED_COMPONENTS>{{"Core": {{"components": {[first, "bad::id"]!r}}}, "Execution": {{"components": {[first, other]!r}}}}}</GROUPED_COMPONENTS>'

    tree = cluster_modules(leaves, components, config, completer=completer)
    assert calls
    assert len(tree) >= 2
    owners = module_file_owners(tree, components)
    expected = {p.name for p in repo.glob("*.go") if not p.name.endswith("_test.go")}
    assert owners.keys() == expected
    assert all(len(owner) == 1 for owner in owners.values())
    validate_module_file_coverage(tree, components)
    assert f"code_tokens={get_module_token_count(components)}" in caplog.text
    assert f"leaf_tokens={get_clustering_input_token_count(leaves, components)}" in caplog.text
    assert "threshold=4000" in caplog.text
    assert f"candidate_files={len(files)}/{len(files)}" in caplog.text


def test_small_repository_keeps_one_page_with_all_file_entry_points(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "types.go").write_text("package tiny\ntype Buffer struct { value int }\n")
    (repo / "methods.go").write_text(
        "package tiny\nfunc (b *Buffer) Read() int { return b.value }\n"
    )
    (repo / "constants.go").write_text("package tiny\nconst Capacity = 16\n")
    config = _config(repo, tmp_path / "out")
    components, leaves = DependencyGraphBuilder(config).build_dependency_graph()
    assert get_module_token_count(components) < config.max_token_per_module
    assert len(component_files(leaves, components)) == 1
    calls = []
    assert cluster_modules(leaves, components, config, completer=lambda p: calls.append(p)) == {}
    assert calls == []
    assert component_files(complete_file_candidates(leaves, components), components) == {
        "types.go",
        "methods.go",
        "constants.go",
    }


def test_size_includes_non_leaf_components_in_same_and_other_files():
    extension = "go"
    leaf = _node(f"types.{extension}::T", "type T {}", "struct")
    method = _node(f"types.{extension}::T.work", "work();\n" * 80)
    other = _node(f"methods.{extension}::T.read", "read();\n" * 100)
    components = {node.id: node for node in (leaf, method, other)}
    assert get_module_token_count(components) == sum(
        count_tokens(n.source_code) for n in components.values()
    )
    assert get_module_token_count(components, {leaf.relative_path}) == count_tokens(
        leaf.source_code
    ) + count_tokens(method.source_code)


@pytest.mark.parametrize(
    ("extension", "source"),
    [
        ("py", "CAPACITY = 16\n"),
        ("js", "export const capacity = 16;\n"),
        ("ts", "export const capacity: number = 16;\n"),
        ("java", "package engine;\n"),
        ("c", "#define CAPACITY 16\n"),
        ("cpp", "#pragma once\n#define CAPACITY 16\n"),
        ("cs", "using System;\n"),
        ("go", "package engine\nconst Capacity = 16\n"),
        ("rs", "const CAPACITY: usize = 16;\n"),
        ("rb", "CAPACITY = 16\n"),
        ("dart", "const capacity = 16;\n"),
        ("scala", "package engine\nval capacity = 16\n"),
        ("kt", "package engine\nconst val CAPACITY = 16\n"),
        ("php", "<?php\nconst CAPACITY = 16;\n"),
        ("zig", "const capacity: usize = 16;\n"),
        ("vue", "<template><div>Engine</div></template>\n"),
    ],
)
def test_files_without_architectural_symbols_survive_each_language_parser(
    tmp_path, extension, source
):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = f"source.{extension}"
    (repo / path).write_text(source)
    components, leaves = DependencyGraphBuilder(
        _config(repo, tmp_path / "out")
    ).build_dependency_graph()
    assert component_files(list(components), components) == {path}
    assert component_files(complete_file_candidates(leaves, components), components) == {path}
    assert get_module_token_count(components) > 0


def test_capped_artifact_does_not_hide_symbol_free_source_code(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = "CAPACITY = " + repr("x" * 30000) + "\n"
    (repo / "setup.py").write_text(source)
    config = _config(repo, tmp_path / "out")
    config.artifacts_enabled = True
    components, leaves = DependencyGraphBuilder(config).build_dependency_graph()
    assert components["setup.py::__file__"].source_code == source
    assert len(components["setup.py::setup.py"].source_code) < len(source)
    assert "setup.py::__file__" in complete_file_candidates(leaves, components)
    assert get_module_token_count(components) >= count_tokens(source)


@pytest.mark.parametrize(
    ("language", "included", "excluded"),
    [
        ("go", "source.go", "vendor/source.go"),
        ("zig", "source.zig", "zig-out/source.zig"),
        ("php", "source.php", "templates/source.php"),
    ],
)
def test_file_exclusions_do_not_import_parsers(monkeypatch, language, included, excluded):
    from codewiki.src.be.dependency_analyzer.utils.file_selection import is_skipped_source_file

    original_import = builtins.__import__

    def without_grammars(name, *args, **kwargs):
        if name.startswith(("tree_sitter", "codewiki.src.be.dependency_analyzer.analyzers")):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_grammars)
    assert not is_skipped_source_file({"language": language, "path": included}, "/repo")
    assert is_skipped_source_file({"language": language, "path": excluded}, "/repo")


def test_symbol_free_file_survives_missing_php_grammar(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = "<?php\nconst CAPACITY = 16;\n"
    (repo / "source.php").write_text(source)
    templates = repo / "templates"
    templates.mkdir()
    (templates / "view.php").write_text("<?php echo 'view';\n")
    original_import = builtins.__import__

    def without_php(name, *args, **kwargs):
        if name in ("tree_sitter_php", "codewiki.src.be.dependency_analyzer.analyzers.php"):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_php)
    components, leaves = DependencyGraphBuilder(
        _config(repo, tmp_path / "out")
    ).build_dependency_graph()
    assert component_files(list(components), components) == {"source.php"}
    assert components["source.php::__file__"].source_code == source
    assert complete_file_candidates(leaves, components) == ["source.php::__file__"]


def test_recursive_size_uses_child_files_not_whole_graph(tmp_path):
    components = {
        cid: _node(cid, source)
        for cid, source in {
            "a.go::A": "small",
            "b.go::B": "small",
            "large.go::Large": "large();\n" * 1000,
            "large.go::Large.method": "method();\n" * 1000,
        }.items()
    }
    config = _config(tmp_path, tmp_path / "out", 100)
    tree = {"Small": {"components": ["a.go::A", "b.go::B"], "children": {}}}
    calls = []
    result = cluster_modules(
        ["a.go::A", "b.go::B"],
        components,
        config,
        tree,
        "Small",
        ["Small"],
        completer=lambda p: calls.append(p),
    )
    assert result == {} and calls == []


def test_indivisible_file_does_not_call_llm_or_recurse(tmp_path):
    nodes = [_node(f"one.go::T.m{i}", "work();\n" * 100) for i in range(20)]
    components = {n.id: n for n in nodes}
    calls = []
    assert (
        cluster_modules(
            [nodes[0].id],
            components,
            _config(tmp_path, tmp_path / "out", 1),
            completer=lambda p: calls.append(p),
        )
        == {}
    )
    assert calls == []


def test_batches_respect_limits_and_reunite_a_file_split_across_batches(tmp_path):
    ids = [f"one.go::S{i}" for i in range(8)] + ["two.go::T"]
    components = {cid: _node(cid) for cid in ids}
    config = _config(tmp_path, tmp_path / "out", 1)
    config.max_leaf_nodes_per_cluster = 3
    calls = []

    def completer(prompt):
        section = prompt.split("<POTENTIAL_CORE_COMPONENTS>")[1].split(
            "</POTENTIAL_CORE_COMPONENTS>"
        )[0]
        batch = [line.strip() for line in section.splitlines() if line.startswith("\t")]
        calls.append(batch)
        return f'<GROUPED_COMPONENTS>{{"Group{len(calls)}": {{"components": {batch!r}}}}}</GROUPED_COMPONENTS>'

    tree = cluster_modules(ids, components, config, completer=completer)
    assert len(calls) > 1
    assert all(len(batch) <= 3 for batch in calls)
    assert len(tree) == 2
    validate_module_file_coverage(tree, components)
    assert all(len(owners) == 1 for owners in module_file_owners(tree, components).values())


def test_partition_restores_omissions_and_moves_whole_files():
    ids = ["src/a.py::A", "src/a.py::A.m", "src/b.py::B", "other/c.py::C"]
    components = {cid: _node(cid) for cid in ids}
    proposal = {
        "Zulu": {"components": [ids[0], ids[0], "unknown::bad"]},
        "Alpha": {"components": [ids[1]]},
        "Other": {"components": [ids[3]]},
        "Broken": None,
    }
    tree = normalize_module_partition(proposal, ids, components)
    assert tree["Alpha"]["components"] == ids[:3]
    assert "Zulu" not in tree
    validate_module_file_coverage(tree, components)
    with pytest.raises(ValueError, match="missing="):
        validate_module_file_coverage({"A": {"components": [ids[0]]}}, components)
    with pytest.raises(ValueError, match="duplicate_owners"):
        validate_module_file_coverage(
            {"A": {"components": ids}, "B": {"components": [ids[1]]}}, components
        )


def test_delegation_covers_parent_without_stealing_siblings():
    ids = ["a.go::A", "a.go::A.m", "b.go::B.m", "sibling.go::S"]
    components = {cid: _node(cid) for cid in ids}
    specs = {"First": [ids[0]], "Second": [ids[1], ids[3]]}
    normalized = prepare_submodule_partition(specs, ids[:3], components, {})
    assert sorted(cid for group in normalized.values() for cid in group) == sorted(ids[:3])
    assert len(normalized) == 1
    assert (
        prepare_submodule_partition(
            specs, ids[:3], components, {"Existing": {"components": ids[:3]}}
        )
        == {}
    )


@pytest.mark.parametrize("backend_kind", ["pydantic", "caw"])
@pytest.mark.parametrize("max_depth", [1, 2])
def test_small_whole_repo_agent_has_all_files_and_optional_delegate(
    tmp_path, monkeypatch, backend_kind, max_depth
):
    from codewiki.src.be.backend import UsageLedger

    repo = tmp_path / "repo"
    out = tmp_path / "out"
    repo.mkdir()
    out.mkdir()
    components = {}
    for path, name in (("types.go", "Buffer"), ("methods.go", "Buffer.Read")):
        file = repo / path
        file.write_text("package tiny\n")
        node = _node(f"{path}::{name}", "small")
        node.file_path = str(file)
        components[node.id] = node
    (out / "module_tree.json").write_text("{}")
    config = _config(repo, out)
    config.max_depth = max_depth
    captured = {}

    if backend_kind == "pydantic":
        from codewiki.src.be import pydantic_ai_backend as mod

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                captured["can_delegate"] = any(
                    tool.name == "generate_sub_module_documentation" for tool in kwargs["tools"]
                )

            async def run(self, prompt, deps, **kwargs):
                captured["ids"] = deps.module_component_ids()
                (out / "tiny.md").write_text("# Tiny\n\nModule documentation.")
                return SimpleNamespace(output="ok")

        monkeypatch.setattr(mod, "Agent", FakeAgent)
        backend = object.__new__(mod.PydanticAIBackend)
        backend._fallback_models = None
        backend._custom_instructions = ""
        backend._usage_ledger = UsageLedger()
    else:
        pytest.importorskip("caw")
        from codewiki.src.be import caw_backend as mod

        class FakeCawAgent:
            def __init__(self, **kwargs):
                toolkit = kwargs["tool_servers"][0]
                captured["can_delegate"] = toolkit._allow_subagent
                captured["ids"] = toolkit._deps.module_component_ids()

            def completion(self, prompt):
                (out / "tiny.md").write_text("# Tiny\n\nModule documentation.")
                return SimpleNamespace(
                    total_usage=None, model="test", num_turns=1, total_tool_calls=0
                )

        monkeypatch.setattr(mod, "CawAgent", FakeCawAgent)
        backend = object.__new__(mod.CawBackend)
        backend._caw_provider = "codex"
        backend._model = "test"
        backend._repo_root = str(repo)

    backend._config = config
    asyncio.run(backend.run_module_agent("tiny", components, ["types.go::Buffer"], [], str(out)))
    assert captured["can_delegate"] == (max_depth > 1)
    assert component_files(captured["ids"], components) == {"types.go", "methods.go"}
    assert json.loads((out / "module_tree.json").read_text()) == {}


@pytest.mark.parametrize("backend_kind", ["pydantic", "caw"])
def test_delegate_normalizes_files_before_running_subagents(tmp_path, monkeypatch, backend_kind):
    from codewiki.src.be.agent_tools.deps import CodeWikiDeps
    from codewiki.src.be.agent_tools import generate_sub_module_documentations as tool

    ids = ["a.go::A", "a.go::A.work", "b.go::B.work", "other.go::Other"]
    components = {cid: _node(cid) for cid in ids}
    config = _config(tmp_path, tmp_path / "out")
    tree = {"Parent": {"components": ids[:3], "children": {}}, "Sibling": {"components": ids[3:]}}
    deps = CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components=components,
        path_to_current_module=["Parent"],
        current_module_name="Parent",
        module_tree=tree,
        max_depth=2,
        current_depth=1,
        config=config,
        scope_component_ids=ids[:3],
        custom_instructions="",
    )
    seen = []
    specs = {"One": [ids[0]], "Two": [ids[1], ids[3]]}
    if backend_kind == "pydantic":

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, prompt, deps, **kwargs):
                seen.extend(deps.module_component_ids())
                from codewiki.src.be.doc_layout import module_doc_file

                name = module_doc_file(
                    deps.current_module_name, deps.path_to_current_module, deps.config.layout
                )
                path = Path(deps.absolute_docs_path, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Module\n\nModule documentation.")
                return SimpleNamespace(output="ok")

        monkeypatch.setattr(tool, "Agent", FakeAgent)
        monkeypatch.setattr(tool, "create_fallback_models", lambda config: None)
        asyncio.run(tool.generate_sub_module_documentation(SimpleNamespace(deps=deps), specs))
    else:
        pytest.importorskip("caw")
        from codewiki.src.be.caw_toolkit import CawToolKit

        class Backend:
            def _run_module_agent_sync(self, **kwargs):
                seen.extend(kwargs["core_component_ids"])

        CawToolKit(deps, Backend(), True)._run_sub_modules(specs)
    assert sorted(seen) == sorted(ids[:3])
    assert deps.scope_component_ids == ids[:3]
    assert deps.current_module_name == "Parent" and deps.current_depth == 1
    validate_module_file_coverage(tree, components)
