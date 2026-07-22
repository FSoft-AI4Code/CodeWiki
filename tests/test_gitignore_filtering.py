"""Tests for automatic Git ignore filtering (issue #71)."""

import asyncio
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
