"""Tests for Git ignore filtering and checkout boundaries (issues #71 and #157)."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import codewiki.cli.config_manager as config_manager_module
from codewiki.cli.main import cli
from codewiki.cli.config_manager import ConfigManager
from codewiki.cli.models.config import Configuration
from codewiki.mcp.server import list_tools
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser
from codewiki.src.be.dependency_analyzer.analysis.repo_analyzer import RepoAnalyzer


def _write_source(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def example():\n    return True\n", encoding="utf-8")


def _tree_paths(tree: dict) -> set[str]:
    paths: set[str] = set()

    def visit(node: dict) -> None:
        if node["type"] == "file":
            paths.add(node["path"])
        for child in node.get("children", []):
            visit(child)

    visit(tree)
    return paths


def _analyzed_paths(repo: Path, **kwargs) -> set[str]:
    result = RepoAnalyzer(include_patterns=["*.py"], **kwargs).analyze_repository_structure(
        str(repo)
    )
    return _tree_paths(result["file_tree"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path) -> None:
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Checkout Test",
        "-c",
        "user.email=checkout@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )


@pytest.fixture
def nested_checkouts(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("Git is required to create the fixture")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "helper.py").write_text("def helper():\n    return 1\n")
    (repo / "app.py").write_text("from helper import helper\ndef run():\n    return helper()\n")
    _commit(repo)
    _git(repo, "worktree", "add", "-qb", "fixture-worktree", ".claude/worktrees/feature")
    _git(repo, "clone", "-q", ".", "copies/other")
    _write_source(repo / "untracked.py")
    for relative in (".claude/worktrees/feature", "copies/other"):
        (repo / relative / ".gitignore").write_text("secret.py\n")
        _write_source(repo / relative / "secret.py")
    return repo


@pytest.mark.parametrize("use_gitignore", [True, False])
@pytest.mark.parametrize("git_available", [True, False])
def test_nested_checkouts_are_pruned_before_traversal(
    nested_checkouts: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_gitignore: bool,
    git_available: bool,
) -> None:
    repo = nested_checkouts
    skipped = {repo / ".claude/worktrees/feature", repo / "copies/other"}
    original_scandir = os.scandir
    original_iterdir = Path.iterdir

    def scandir(path):
        assert Path(path) not in skipped, f"Fallback entered nested checkout: {path}"
        return original_scandir(path)

    def iterdir(path):
        assert path not in skipped, f"Source walk entered nested checkout: {path}"
        return original_iterdir(path)

    monkeypatch.setattr("os.scandir", scandir)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    if not git_available:
        monkeypatch.setattr(
            "codewiki.src.be.dependency_analyzer.analysis.repo_analyzer.shutil.which",
            lambda _name: None,
        )
    assert _analyzed_paths(repo, use_gitignore=use_gitignore) == {
        "app.py",
        "helper.py",
        "untracked.py",
    }


@pytest.mark.parametrize("relative", [".claude/worktrees/feature", "copies/other"])
def test_selected_nested_checkout_is_analyzable(nested_checkouts: Path, relative: str) -> None:
    assert _analyzed_paths(nested_checkouts / relative) == {"app.py", "helper.py"}


def test_nested_checkout_nodes_do_not_reach_dependency_graph(nested_checkouts: Path) -> None:
    components = DependencyParser(str(nested_checkouts)).parse_repository()
    assert {node.relative_path for node in components.values()} == {
        "app.py",
        "helper.py",
        "untracked.py",
    }
    assert components["app.py::run"].depends_on == {"helper.py::helper"}


def test_plain_directory_prunes_nested_checkout(nested_checkouts: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _write_source(plain / "main.py")
    _git(plain, "clone", "-q", str(nested_checkouts), "nested")
    assert _analyzed_paths(plain) == {"main.py"}


@pytest.mark.parametrize("git_available", [True, False])
def test_declared_submodules_and_nested_submodules_are_preserved(
    nested_checkouts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: bool
) -> None:
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    _git(leaf, "init", "-q")
    _write_source(leaf / "leaf.py")
    _commit(leaf)
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _git(dependency, "init", "-q")
    _write_source(dependency / "dep.py")
    _git(
        dependency, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(leaf), "child"
    )
    _commit(dependency)
    repo = nested_checkouts
    submodule = repo / "packages" / "registered dependency"
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(dependency),
        "packages/registered dependency",
    )
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    _git(submodule, "clone", "-q", str(leaf), "accidental")
    # Quoting and inline comments are valid in Git configuration files.
    (repo / ".gitmodules").write_text(
        '[submodule "registered"]\n path = "packages/registered dependency" # keep\n'
    )
    if not git_available:
        monkeypatch.setattr(
            "codewiki.src.be.dependency_analyzer.analysis.repo_analyzer.shutil.which",
            lambda _name: None,
        )
    assert _analyzed_paths(repo) == {
        "app.py",
        "helper.py",
        "untracked.py",
        "packages/registered dependency/dep.py",
        "packages/registered dependency/child/leaf.py",
    }
    assert _analyzed_paths(repo / "packages") == {
        "registered dependency/dep.py",
        "registered dependency/child/leaf.py",
    }
    assert _analyzed_paths(submodule) == {"dep.py", "child/leaf.py"}
    assert _analyzed_paths(repo, exclude_patterns=["packages/registered dependency"]) == {
        "app.py",
        "helper.py",
        "untracked.py",
    }


@pytest.mark.parametrize(
    "declarations",
    [
        '[submodule "copy"]\n path = copies/other\n[broken',
        '[submodule "copy"]\n path = "copies/other\n',
        '[submodule "copy"]\n path = "copies/other\\q"\n',
    ],
)
def test_malformed_submodule_declarations_do_not_exempt_checkouts(
    nested_checkouts: Path, declarations: str, caplog: pytest.LogCaptureFixture
) -> None:
    (nested_checkouts / ".gitmodules").write_text(declarations)
    assert _analyzed_paths(nested_checkouts) == {"app.py", "helper.py", "untracked.py"}
    assert "Could not read submodule declarations" in caplog.text


def test_unreadable_submodule_declarations_do_not_abort_analysis(
    nested_checkouts: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    declarations = nested_checkouts / ".gitmodules"
    declarations.write_text('[submodule "copy"]\n path = copies/other\n')
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == declarations:
            raise PermissionError("fixture")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert _analyzed_paths(nested_checkouts) == {"app.py", "helper.py", "untracked.py"}
    assert "Could not read submodule declarations" in caplog.text


def test_escaping_submodule_declarations_are_rejected(
    nested_checkouts: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = nested_checkouts
    (repo / ".gitmodules").write_text(
        '[submodule "outside"]\n path = ../outside\n'
        f'[submodule "absolute"]\n path = "{repo / "copies/other"}"\n'
    )
    assert _analyzed_paths(repo) == {"app.py", "helper.py", "untracked.py"}
    assert "Skipping escaping submodule path" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="Windows does not support trailing spaces in paths")
def test_quoted_submodule_path_preserves_significant_whitespace(nested_checkouts: Path) -> None:
    repo = nested_checkouts
    (repo / "copies/other").rename(repo / " dependency ")
    (repo / ".gitmodules").write_text('[submodule "copy"]\n path = " dependency " # keep\n')
    assert _analyzed_paths(repo) == {
        "app.py",
        "helper.py",
        "untracked.py",
        " dependency /app.py",
        " dependency /helper.py",
        # The parent Git ignore query does not recurse into submodules.
        " dependency /secret.py",
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_gitignore_is_default_and_keeps_tracked_matches(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(
        "ignored.py\nignored_dir/\n*.generated.py\n!keep.generated.py\ntracked.py\n",
        encoding="utf-8",
    )
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".gitignore").write_text(
        "secret.py\n!visible.py\n",
        encoding="utf-8",
    )

    for relative in (
        "ignored.py",
        "ignored_dir/child.py",
        "drop.generated.py",
        "keep.generated.py",
        "kept.py",
        "nested/secret.py",
        "nested/visible.py",
        "tracked.py",
    ):
        _write_source(tmp_path / relative)

    _git(tmp_path, "add", "-f", "tracked.py")

    assert _analyzed_paths(tmp_path) == {
        "keep.generated.py",
        "kept.py",
        "nested/visible.py",
        "tracked.py",
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_gitignore_can_be_disabled_without_disabling_explicit_excludes(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _write_source(tmp_path / "ignored.py")
    _write_source(tmp_path / "explicit.py")

    assert _analyzed_paths(
        tmp_path,
        use_gitignore=False,
        exclude_patterns=["explicit.py"],
    ) == {"ignored.py"}


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_monorepo_subdirectory_honors_parent_gitignore(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    app = tmp_path / "packages" / "app"
    _write_source(app / "ignored.py")
    _write_source(app / "kept.py")
    (tmp_path / ".gitignore").write_text(
        "packages/app/ignored.py\n",
        encoding="utf-8",
    )

    assert _analyzed_paths(app) == {"kept.py"}


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_wholly_ignored_monorepo_subdirectory_is_empty(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    app = tmp_path / "packages" / "app"
    _write_source(app / "ignored.py")
    (tmp_path / ".gitignore").write_text("packages/app/\n", encoding="utf-8")

    assert _analyzed_paths(app) == set()

    _write_source(app / "tracked.py")
    _git(tmp_path, "add", "-f", "packages/app/tracked.py")
    assert _analyzed_paths(app) == {"tracked.py"}


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_ignored_sources_do_not_reach_dependency_graph(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _write_source(tmp_path / "ignored.py")
    _write_source(tmp_path / "kept.py")

    components = DependencyParser(str(tmp_path)).parse_repository()

    assert {node.relative_path for node in components.values()} == {"kept.py"}


def test_non_git_fallback_supports_nested_rules_and_negation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codewiki.src.be.dependency_analyzer.analysis.repo_analyzer.shutil.which",
        lambda _name: None,
    )
    (tmp_path / ".gitignore").write_text(
        "ignored.py\n*.generated.py\n!keep.generated.py\n",
        encoding="utf-8",
    )
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".gitignore").write_text(
        "secret.py\n!visible.py\n",
        encoding="utf-8",
    )
    for relative in (
        "ignored.py",
        "drop.generated.py",
        "keep.generated.py",
        "kept.py",
        "nested/secret.py",
        "nested/visible.py",
    ):
        _write_source(tmp_path / relative)

    assert _analyzed_paths(tmp_path) == {
        "keep.generated.py",
        "kept.py",
        "nested/visible.py",
    }


def test_unreadable_fallback_gitignore_does_not_abort_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codewiki.src.be.dependency_analyzer.analysis.repo_analyzer.shutil.which",
        lambda _name: None,
    )
    (tmp_path / ".gitignore").write_bytes(b"\xff\xfe")
    _write_source(tmp_path / "kept.py")

    assert _analyzed_paths(tmp_path) == {"kept.py"}


def test_configuration_defaults_and_round_trips_gitignore_setting() -> None:
    old_config = Configuration.from_dict(
        {"base_url": "https://example.com", "main_model": "main", "cluster_model": "cluster"}
    )
    assert old_config.use_gitignore is True

    old_config.use_gitignore = False
    serialized = old_config.to_dict()
    assert serialized["use_gitignore"] is False
    assert Configuration.from_dict(serialized).use_gitignore is False


def test_config_manager_persists_disabled_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEWIKI_NO_KEYRING", "1")
    monkeypatch.setattr(config_manager_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_manager_module, "CREDENTIALS_FILE", tmp_path / "credentials.json")

    ConfigManager().save(use_gitignore=False)
    loaded = ConfigManager()

    assert loaded.load() is True
    assert loaded.get_config().use_gitignore is False


def test_cli_exposes_runtime_and_persistent_gitignore_switches() -> None:
    runner = CliRunner()
    generate_help = runner.invoke(cli, ["generate", "--help"])
    config_help = runner.invoke(cli, ["config", "set", "--help"])

    assert generate_help.exit_code == 0
    assert "--use-gitignore / --no-gitignore" in generate_help.output
    assert config_help.exit_code == 0
    assert "--use-gitignore / --no-gitignore" in config_help.output


def test_mcp_analysis_tools_default_to_gitignore_enabled() -> None:
    tools = {tool.name: tool for tool in asyncio.run(list_tools())}

    for tool_name in ("analyze_repo", "generate_docs"):
        schema = tools[tool_name].inputSchema["properties"]["use_gitignore"]
        assert schema == {
            "type": "boolean",
            "description": "Apply Git ignore rules before analysis (default: true)",
            "default": True,
        }
