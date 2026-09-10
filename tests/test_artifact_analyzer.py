"""Tests for artifact-aware generation.

Build, CI, container, packaging, manifest, config, schema and script files
("artifacts") used to be dropped before any Node was created, so CodeWiki
never documented how a system is built and shipped. These tests cover the
classifier, the file-walk whitelist, node/unit/edge emission, caps, leaf
selection, the user prompt and the guaranteed fallback module.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codewiki.src.be.cluster_modules import (
    ARTIFACT_MODULE_NAME,
    ensure_artifact_module,
    format_potential_core_components,
)
from codewiki.src.be.dependency_analyzer.analysis.repo_analyzer import RepoAnalyzer
from codewiki.src.be.dependency_analyzer.analyzers.artifact import (
    TRUNCATION_MARKER,
    ArtifactOptions,
    classify_artifact,
    render_artifact_index,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser
from codewiki.src.be.dependency_analyzer.leaf_selection import compute_valid_leaf_types
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.dependency_analyzer.topo_sort import (
    build_graph_from_components,
    get_leaf_nodes,
)
from codewiki.src.be.prompt_template import USER_PROMPT, format_user_prompt

# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/cli.py", "def main():\n    return 0\n\n\ndef helper():\n    return 1\n")
    _write(tmp_path, "pkg/core.py", "class Engine:\n    def run(self):\n        return 1\n")
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "mini"\nversion = "0.1"\n\n[project.scripts]\nmytool = "pkg.cli:main"\n',
    )
    _write(
        tmp_path,
        "package.json",
        '{\n  "name": "mini",\n  "main": "pkg/index.js",\n  "scripts": {\n    "build": "node scripts/build.js",\n    "test": "npm run build && node test.js"\n  }\n}\n',
    )
    _write(
        tmp_path,
        "scripts/build.js",
        "function build() { return 1; }\nmodule.exports = { build };\n",
    )
    _write(
        tmp_path,
        "Makefile",
        "VAR := x\n\n.PHONY: build test\n\nbuild:\n\tpython -m pkg.cli\n\ntest: build\n\tpython pkg/cli.py\n\n%.o: %.c\n\t$(CC) -c $<\n",
    )
    _write(
        tmp_path,
        "Dockerfile",
        'FROM python:3.12 AS builder\nCOPY pkg/cli.py /app/cli.py\nRUN make build\n\nFROM python:3.12-slim\nCOPY --from=builder /app /app\nENTRYPOINT ["python", "pkg/cli.py"]\n',
    )
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "name: CI\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: ruff check .\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n",
    )
    _write(tmp_path, ".github/ISSUE_TEMPLATE/bug.md", "# bug\n")
    _write(tmp_path, "README.md", "# mini\n")
    _write(tmp_path, "docs/guide.md", "# guide\n")
    _write(tmp_path, "config/big.yaml", "key: value\n" * 3000)  # > 16 KB
    _write(tmp_path, "conftest.py", "import pytest\n")
    _write(tmp_path, "tests/conftest.py", "import pytest\n")
    _write(tmp_path, "package-lock.json", '{"lockfileVersion": 3}\n')
    _write(tmp_path, "pytest.ini", "[pytest]\naddopts = -q\n")
    return tmp_path


def _artifact_nodes(components: dict[str, Node]) -> dict[str, Node]:
    return {k: v for k, v in components.items() if v.component_type == "artifact"}


# --------------------------------------------------------------------------- #
# 1. classifier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel_path, expected",
    [
        (".github/workflows/ci.yml", "ci"),
        (".gitlab-ci.yml", "ci"),
        ("Dockerfile", "container"),
        ("docker/Dockerfile.erb", "container"),
        ("docker-compose.yml", "container"),
        ("docker/entrypoint.sh", "container"),
        ("package.json", "manifest"),
        ("packages/foo/package.json", "manifest"),
        ("pyproject.toml", "manifest"),
        ("setup.cfg", "manifest"),
        ("requirements-dev.txt", "manifest"),
        ("packages/deb/control", "packaging"),
        ("pkg/logstash.service", "packaging"),
        ("Makefile", "build"),
        ("BUILD.gn", "build"),
        ("rakelib/artifacts.rake", "build"),
        ("pytest.ini", "test_infra"),
        ("conftest.py", "test_infra"),
        ("proto/msg.proto", "schema"),
        ("config/jvm.options", "config"),
        ("ruff.toml", "config"),
        ("deep/nested/dir/settings.yml", None),
        ("bin/run.sh", "script"),
        ("README.md", None),
        ("docs/guide.md", None),
        ("package-lock.json", None),
        (".github/ISSUE_TEMPLATE/bug.md", None),
        ("CODEOWNERS", None),
        ("src/main.py", None),
    ],
)
def test_classify_artifact_table(rel_path: str, expected: str | None) -> None:
    name = rel_path.rsplit("/", 1)[-1]
    assert classify_artifact(rel_path, name, 100, ArtifactOptions()) == expected


def test_classify_prose_and_exclude() -> None:
    prose = ArtifactOptions(with_prose=True)
    assert classify_artifact("README.md", "README.md", 10, prose) == "prose"
    assert classify_artifact("docs/guide.md", "guide.md", 10, prose) == "prose"
    assert classify_artifact("pkg/notes.md", "notes.md", 10, prose) is None
    excl = ArtifactOptions(exclude_patterns=["docker/data/*"])
    assert classify_artifact("docker/data/huge.yml", "huge.yml", 10, excl) is None
    assert (
        classify_artifact("docker/data/huge.yml", "huge.yml", 10, ArtifactOptions()) == "container"
    )
    assert classify_artifact("Dockerfile", "Dockerfile", 0, ArtifactOptions()) is None


# --------------------------------------------------------------------------- #
# 2. file walk whitelist
# --------------------------------------------------------------------------- #


def _tree_paths(tree: dict) -> set[str]:
    out: set[str] = set()

    def _walk(node: dict | None) -> None:
        if not node:
            return
        if node["type"] == "file":
            out.add(node["path"].replace(os.sep, "/"))
        for child in node.get("children", []) or []:
            _walk(child)

    _walk(tree)
    return out


def test_repo_analyzer_whitelist(mini_repo: Path) -> None:
    paths = _tree_paths(
        RepoAnalyzer(use_gitignore=False).analyze_repository_structure(str(mini_repo))["file_tree"]
    )
    assert {
        ".github/workflows/ci.yml",
        "pytest.ini",
        "Dockerfile",
        "Makefile",
        "pyproject.toml",
    } <= paths
    assert "tests/conftest.py" not in paths
    assert ".github/ISSUE_TEMPLATE/bug.md" not in paths
    # user excludes still win over the whitelist
    paths_user = _tree_paths(
        RepoAnalyzer(
            exclude_patterns=[".github"], use_gitignore=False
        ).analyze_repository_structure(str(mini_repo))["file_tree"]
    )
    assert ".github/workflows/ci.yml" not in paths_user


# --------------------------------------------------------------------------- #
# 3. nodes and units
# --------------------------------------------------------------------------- #


def test_parse_repository_emits_artifact_nodes_and_units(mini_repo: Path) -> None:
    components = DependencyParser(
        str(mini_repo), use_gitignore=False, artifact_options=ArtifactOptions()
    ).parse_repository()
    artifacts = _artifact_nodes(components)
    ids = set(artifacts)
    expected = {
        "Dockerfile::Dockerfile",
        "Dockerfile::builder",
        "Makefile::Makefile",
        "Makefile::build",
        "Makefile::test",
        "package.json::package.json",
        "package.json::build",
        "pyproject.toml::mytool",
        ".github/workflows/ci.yml::ci.yml",
        ".github/workflows/ci.yml::lint",
        ".github/workflows/ci.yml::test",
        "pytest.ini::pytest.ini",
        "conftest.py::conftest.py",
    }
    missing = expected - ids
    assert not missing, f"missing artifact ids: {sorted(missing)}"
    assert "Makefile::.PHONY" not in ids and "Makefile::VAR" not in ids
    assert not any(i.startswith(("README", "docs/")) for i in ids)
    assert artifacts["Dockerfile::Dockerfile"].artifact_class == "container"
    assert artifacts["Makefile::build"].artifact_class == "build"
    assert artifacts[".github/workflows/ci.yml::lint"].artifact_class == "ci"
    assert artifacts["Makefile::test"].source_code.startswith("test: build\n\tpython pkg/cli.py")
    # code side is untouched
    assert (
        "pkg/cli.py::main" in components
        and components["pkg/cli.py::main"].component_type == "function"
    )
    # a parser without options keeps the code-only graph
    plain = DependencyParser(str(mini_repo), use_gitignore=False).parse_repository()
    assert not _artifact_nodes(plain)


# --------------------------------------------------------------------------- #
# 4. edges
# --------------------------------------------------------------------------- #


def test_edges_resolve_only_to_known_ids(mini_repo: Path) -> None:
    components = DependencyParser(
        str(mini_repo), use_gitignore=False, artifact_options=ArtifactOptions()
    ).parse_repository()
    assert "pkg/cli.py::main" in components["pyproject.toml::mytool"].depends_on
    assert "pkg/cli.py::main" in components["Makefile::build"].depends_on  # python -m pkg.cli
    assert "pkg/cli.py::main" in components["Makefile::test"].depends_on  # pkg/cli.py path
    assert "Makefile::build" in components["Makefile::test"].depends_on  # prerequisite
    assert "Makefile::test" in components[".github/workflows/ci.yml::test"].depends_on
    assert "Makefile::build" in components["Dockerfile::builder"].depends_on  # RUN make build
    assert "pkg/cli.py::main" in components["Dockerfile::builder"].depends_on  # COPY pkg/cli.py
    assert "Dockerfile::builder" in components["Dockerfile::stage_1"].depends_on  # --from=builder
    assert "scripts/build.js::build" in components["package.json::build"].depends_on
    assert "package.json::build" in components["package.json::test"].depends_on  # npm run build
    # file nodes point at their units
    assert "Makefile::build" in components["Makefile::Makefile"].depends_on
    # every edge target exists, and no code node depends on an artifact
    for node in components.values():
        for dep in node.depends_on:
            assert dep in components, (node.id, dep)
            if node.component_type != "artifact":
                assert components[dep].component_type != "artifact", (node.id, dep)


# --------------------------------------------------------------------------- #
# 5. caps
# --------------------------------------------------------------------------- #


def test_caps(mini_repo: Path) -> None:
    for i in range(45):
        _write(mini_repo, f"config/c{i:02d}.yaml", f"n: {i}\n")
    parser = DependencyParser(
        str(mini_repo), use_gitignore=False, artifact_options=ArtifactOptions()
    )
    components = parser.parse_repository()
    big = components["config/big.yaml::big.yaml"]
    marker_prefix = TRUNCATION_MARKER.split("{")[0]
    assert marker_prefix in big.source_code
    assert len(big.source_code) < 16_384 + len(TRUNCATION_MARKER) + 32
    config_files = [
        n
        for n in _artifact_nodes(components).values()
        if n.artifact_class == "config" and n.node_type == "artifact_file"
    ]
    assert len(config_files) == 40
    assert (
        len(parser.artifact_index["classes"]["config"]["omitted_by_class_cap"]) == 6
    )  # 46 config files - 40
    # a tiny budget keeps manifests (highest priority) and records what was skipped
    tight = DependencyParser(
        str(mini_repo), use_gitignore=False, artifact_options=ArtifactOptions(token_budget=120)
    )
    tight_components = tight.parse_repository()
    tight_artifacts = _artifact_nodes(tight_components)
    # manifests are loaded first; the big config file no longer fits (small
    # later files may still slip into the leftover budget, by design)
    assert any(n.artifact_class == "manifest" for n in tight_artifacts.values())
    assert "config/big.yaml::big.yaml" not in tight_artifacts
    assert "config/big.yaml" in tight.artifact_index["classes"]["config"]["not_loaded_budget"]
    assert tight.artifact_index["tokens_used"] <= 120


# --------------------------------------------------------------------------- #
# 6. leaf selection
# --------------------------------------------------------------------------- #


def _node(node_id: str, component_type: str, deps: set[str] | None = None) -> Node:
    path = node_id.split("::")[0]
    return Node(
        id=node_id,
        name=node_id.split("::")[-1],
        component_type=component_type,
        file_path=path,
        relative_path=path,
        depends_on=set(deps or set()),
        source_code="x",
    )


def test_leaf_types_and_pruning() -> None:
    assert "artifact" in compute_valid_leaf_types({})
    components: dict[str, Node] = {}
    for i in range(420):
        components[f"src/m{i}.py::C{i}"] = _node(f"src/m{i}.py::C{i}", "class")
    components["src/x.py::X"] = _node("src/x.py::X", "class")
    components["src/z.py::Z"] = _node("src/z.py::Z", "class")
    components["src/y.py::Y"] = _node("src/y.py::Y", "class", {"src/z.py::Z"})
    components["Dockerfile::Dockerfile"] = _node(
        "Dockerfile::Dockerfile", "artifact", {"src/x.py::X"}
    )
    leaves = set(get_leaf_nodes(build_graph_from_components(components), components))
    assert "src/x.py::X" in leaves  # referenced only by an artifact: kept
    assert "Dockerfile::Dockerfile" in leaves
    assert "src/z.py::Z" not in leaves  # referenced by code: pruned
    assert "src/y.py::Y" in leaves


# --------------------------------------------------------------------------- #
# 7. user prompt
# --------------------------------------------------------------------------- #


def test_format_user_prompt_with_artifacts() -> None:
    dockerfile = Node(
        id="Dockerfile::Dockerfile",
        name="Dockerfile",
        component_type="artifact",
        file_path="/nonexistent/Dockerfile",
        relative_path="Dockerfile",
        source_code='FROM python:3.12\nCMD ["python"]\n',
        node_type="artifact_file",
        artifact_class="container",
    )
    stage = Node(
        id="Dockerfile::runtime",
        name="runtime",
        component_type="artifact",
        file_path="/nonexistent/Dockerfile",
        relative_path="Dockerfile",
        source_code="FROM python:3.12\n",
        node_type="artifact_unit",
        artifact_class="container",
    )
    components = {dockerfile.id: dockerfile, stage.id: stage}
    tree = {"Build": {"path": ".", "components": list(components), "children": {}}}
    prompt = format_user_prompt("Build", list(components), components, tree)
    assert "```dockerfile\nFROM python:3.12\nCMD" in prompt
    assert "<REPOSITORY_ARTIFACTS>" in prompt and "## container (1 files)" in prompt
    assert "stages/services: runtime" in prompt
    assert "# Error reading file" not in prompt  # capped source used, file never re-read

    # code-only components: no artifact section, unknown extension does not raise
    code = Node(
        id="cfg/app.yml::app",
        name="app",
        component_type="class",
        file_path="/nonexistent/app.yml",
        relative_path="cfg/app.yml",
        source_code="a: 1",
    )
    prompt2 = format_user_prompt(
        "M",
        [code.id],
        {code.id: code},
        {"M": {"path": "", "components": [code.id], "children": {}}},
    )
    assert "<REPOSITORY_ARTIFACTS>" not in prompt2
    assert "```yaml" in prompt2
    # MCP contract: USER_PROMPT still has exactly the three original placeholders
    USER_PROMPT.format(module_name="m", module_tree="t", formatted_core_component_codes="c")


def test_cluster_input_tags_artifact_files() -> None:
    art = _node("Makefile::Makefile", "artifact")
    art.artifact_class = "build"
    code = _node("src/a.py::A", "class")
    ids_only, _ = format_potential_core_components([art.id, code.id], {art.id: art, code.id: code})
    assert "# Makefile (artifact: build)\n\tMakefile::Makefile" in ids_only
    assert "# src/a.py\n\tsrc/a.py::A" in ids_only


# --------------------------------------------------------------------------- #
# 8. guaranteed module
# --------------------------------------------------------------------------- #


def _artifact_components(n: int) -> dict[str, Node]:
    comps: dict[str, Node] = {}
    for i in range(n):
        node = _node(f"ci/w{i}.yml::w{i}.yml", "artifact")
        node.artifact_class = "ci"
        comps[node.id] = node
    comps["src/a.py::A"] = _node("src/a.py::A", "class")
    return comps


def test_ensure_artifact_module() -> None:
    comps = _artifact_components(5)
    leaves = list(comps)
    art_ids = [i for i in leaves if i.startswith("ci/")]
    # 1/5 assigned -> module inserted with the other 4
    tree = {"Core": {"path": "src", "components": ["src/a.py::A", art_ids[0]], "children": {}}}
    out = ensure_artifact_module(tree, leaves, comps)
    assert ARTIFACT_MODULE_NAME in out
    mod = out[ARTIFACT_MODULE_NAME]
    assert set(mod["components"]) == set(art_ids[1:])
    assert mod["children"] == {} and mod["path"]
    # 4/5 assigned -> unchanged
    tree = {"Core": {"path": "src", "components": ["src/a.py::A", *art_ids[:4]], "children": {}}}
    assert ARTIFACT_MODULE_NAME not in ensure_artifact_module(tree, leaves, comps)
    # whole-repo mode -> unchanged
    assert ensure_artifact_module({}, leaves, comps) == {}
    # name collision -> unique variant
    tree = {ARTIFACT_MODULE_NAME: {"path": "", "components": ["src/a.py::A"], "children": {}}}
    out = ensure_artifact_module(tree, leaves, comps)
    inserted = [k for k in out if k != ARTIFACT_MODULE_NAME]
    assert len(inserted) == 1 and inserted[0].startswith(ARTIFACT_MODULE_NAME)


def test_render_artifact_index_empty_for_code_only() -> None:
    assert render_artifact_index({"src/a.py::A": _node("src/a.py::A", "class")}) == ""
