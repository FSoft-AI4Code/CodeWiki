"""Opt-in live checkout-boundary smoke test; never collected by pytest.

Run: .venv/bin/python tests/smoke_test_nested_checkouts.py --provider codex --model gpt-6-luna
Requires an authenticated Codex CLI. Logs and generated artifacts are retained.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from click.testing import CliRunner

import codewiki.cli.config_manager as config_module
from codewiki.cli.main import cli
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.module_scope import validate_module_file_coverage


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def commit(repo: Path) -> None:
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Checkout Smoke",
        "-c",
        "user.email=smoke@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )


def make_fixture(root: Path) -> Path:
    repo, dependency = root / "repo", root / "dependency"
    repo.mkdir()
    dependency.mkdir()
    git(dependency, "init", "-q")
    (dependency / "support.py").write_text(
        '"""Clamp a numeric value to a nonnegative integer."""\n'
        "def clamp(value: int) -> int:\n    return max(0, value)\n"
    )
    commit(dependency)
    git(repo, "init", "-q")
    (repo / "helper.py").write_text(
        '"""Arithmetic helpers for a small calculator."""\n'
        "def helper(value: int) -> int:\n    return value + 1\n"
    )
    (repo / "app.py").write_text(
        '"""Calculator entrypoint."""\nfrom helper import helper\n\n'
        "def run(value: int) -> int:\n    return helper(value)\n"
    )
    commit(repo)
    git(repo, "worktree", "add", "-qb", "smoke-worktree", ".claude/worktrees/feature")
    git(repo, "clone", "-q", ".", "copies/other")
    for directory in (".claude/worktrees/feature", "copies/other"):
        (repo / directory / "sentinel.py").write_text(
            "def nested_checkout_sentinel():\n    return 'must not be documented'\n"
        )
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(dependency),
        "deps/math_support",
    )
    # Commit only the intended parent files and submodule registration.
    git(
        repo,
        "-c",
        "user.name=Checkout Smoke",
        "-c",
        "user.email=smoke@example.invalid",
        "commit",
        "-qm",
        "register dependency",
    )
    (repo / "untracked.py").write_text(
        '"""Format a calculator result."""\n'
        "def format_result(value: int) -> str:\n    return str(value)\n"
    )
    return repo


def verify(output: Path, model: str) -> dict:
    metadata = json.loads((output / "metadata.json").read_text())
    usage = metadata["usage"]
    assert not usage.get("incomplete"), "Generation was marked incomplete"
    assert usage["n_calls"] > 0, "No real model activity recorded"
    calls = usage["calls"]
    assert all(call["provider"] == "codex" for call in calls), calls
    assert all(call["model"] == model for call in calls), calls
    assert any(call["usage_reported"] for call in calls), "No provider usage recorded"
    assert usage["totals"].get("output_tokens", 0) > 0, "No model output recorded"

    tree = json.loads((output / "module_tree.json").read_text())
    # A small repository uses an empty tree and one whole-repository overview.
    assert isinstance(tree, dict), "Invalid module tree"
    overview = output / "overview.md"
    assert overview.is_file() and overview.read_text().strip(), "Missing overview"
    pages = [p for p in output.rglob("*.md") if "temp" not in p.relative_to(output).parts]
    assert pages and all(p.read_text().strip() for p in pages), "Empty documentation page"
    for page in pages:
        text = page.read_text()
        assert "nested_checkout_sentinel" not in text, page
        assert ".claude/worktrees/feature" not in text and "copies/other" not in text, page

    graphs = list((output / "temp/dependency_graphs").glob("*_dependency_graph.json"))
    assert len(graphs) == 1, graphs
    graph = json.loads(graphs[0].read_text())
    paths = {node["relative_path"] for node in graph.values()}
    assert paths == {"app.py", "helper.py", "untracked.py", "deps/math_support/support.py"}, paths
    assert set(graph["app.py::run"]["depends_on"]) == {"helper.py::helper"}
    if tree:
        validate_module_file_coverage(
            tree, {ident: Node.model_validate(node) for ident, node in graph.items()}
        )
    return {
        "provider": "codex",
        "requested_model": model,
        "recorded_models": sorted({call["model"] for call in calls}),
        "model_calls": usage["n_calls"],
        "source_paths": sorted(paths),
        "documentation_pages": [str(p.relative_to(output)) for p in pages],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["codex"], default="codex")
    parser.add_argument("--model", default="gpt-6-luna")
    args = parser.parse_args()
    if not shutil.which("codex") or not shutil.which("git"):
        parser.error("Git and an authenticated Codex CLI must be on PATH")
    root = Path(tempfile.mkdtemp(prefix="codewiki-checkout-smoke-"))
    print(f"Smoke artifacts: {root}", flush=True)
    repo = make_fixture(root)
    output, config_dir = root / "docs", root / "config"
    previous_cwd = Path.cwd()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"CODEWIKI_NO_KEYRING": "1"}))
            for name, value in {
                "CONFIG_DIR": config_dir,
                "CONFIG_FILE": config_dir / "config.json",
                "CREDENTIALS_FILE": config_dir / "credentials.json",
            }.items():
                stack.enter_context(patch.object(config_module, name, value))
            config_module.ConfigManager().save(
                provider=args.provider,
                main_model=args.model,
                cluster_model=args.model,
                fallback_model="",
            )
            os.chdir(repo)
            print(f"Generating with provider={args.provider}, model={args.model}...", flush=True)
            result = CliRunner().invoke(
                cli,
                [
                    "generate",
                    "--output",
                    str(output),
                    "--no-artifacts",
                    "--no-cache",
                    "--min-token-per-module",
                    "1",
                    "--max-tokens",
                    "8192",
                    "--request-limit",
                    "24",
                    "--verbose",
                    "--instructions",
                    "Keep documentation concise, under 300 words per page. "
                    "Do not include Mermaid diagrams.",
                ],
            )
            (root / "generate.log").write_text(result.output)
            if result.exit_code:
                raise RuntimeError(
                    f"Generation failed with exit {result.exit_code}; see {root / 'generate.log'}"
                ) from result.exception
            report = verify(output, args.model)
            (root / "report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2), flush=True)
            print("PASS: live generation preserved repository boundaries", flush=True)
    finally:
        os.chdir(previous_cwd)
        shutil.rmtree(config_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
