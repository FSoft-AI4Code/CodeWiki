"""Opt-in live generation/update cost check. Never collected by pytest.

Run: .venv/bin/python tests/smoke_test_costs.py --env-file /absolute/path/.env
Rates below are synthetic test data, not a provider price quote.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from click.testing import CliRunner
from dotenv import dotenv_values

import codewiki.cli.config_manager as config_module
from codewiki.cli.main import cli


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT, text=True
    )


def verify(report):
    assert report["cost_status"] == "complete", report["unpriced_models"]
    expected = 0
    for call in report["calls"]:
        assert call["usage_reported"]
        rates = call["rates"]
        read, write = call["cache_read_tokens"], call["cache_write_tokens"]
        expected += (
            (call["input_tokens"] - read - write) * rates["input"]
            + read * rates["cache_read"]
            + write * rates["cache_write"]
            + call["output_tokens"] * rates["output"]
        ) / 1_000_000
    assert math.isclose(expected, report["totals"]["cost_usd"], abs_tol=1e-12)
    for grouping in ("by_model", "by_phase"):
        assert math.isclose(
            sum(g.get("cost_usd", 0) for g in report[grouping].values()),
            expected,
            abs_tol=1e-12,
        )


def verify_display(report, output):
    calls = re.search(r"LLM calls:\s+([\d,]+)", output)
    tokens = re.search(r"Tokens used:\s+([\d,]+)", output)
    cost = re.search(r"Est\. cost:\s+\$([\d,.]+)", output)
    assert calls and int(calls[1].replace(",", "")) == report["n_calls"]
    expected_tokens = report["totals"].get("input_tokens", 0) + report["totals"].get(
        "output_tokens", 0
    )
    assert tokens and int(tokens[1].replace(",", "")) == expected_tokens
    expected_cost = report["totals"]["cost_usd"]
    assert cost and math.isclose(
        float(cost[1].replace(",", "")),
        expected_cost,
        abs_tol=0.005 if expected_cost >= 0.01 else 5e-9,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    env = dotenv_values(args.env_file)
    for name in ("BASE_URL", "API_KEY", "MAIN_MODEL", "FALLBACK_MODEL"):
        if not env.get(name):
            parser.error(f"Missing {name} in env file")

    root = Path(tempfile.mkdtemp(prefix="codewiki-cost-smoke-"))
    repo, output, config_dir = root / "repo", root / "docs", root / "config"
    repo.mkdir()
    config_dir.mkdir()
    config_module.CONFIG_DIR = config_dir
    config_module.CONFIG_FILE = config_dir / "config.json"
    config_module.CREDENTIALS_FILE = config_dir / "credentials.json"
    os.environ["CODEWIKI_NO_KEYRING"] = "1"
    manager = config_module.ConfigManager()
    manager.save(
        base_url=env["BASE_URL"],
        api_key=env["API_KEY"],
        main_model=env["MAIN_MODEL"],
        fallback_model=env["FALLBACK_MODEL"],
        cluster_model=env["MAIN_MODEL"],
        provider="openai-compatible",
    )
    runner = CliRunner()
    for model in {env["MAIN_MODEL"], env["FALLBACK_MODEL"]}:
        result = runner.invoke(
            cli,
            [
                "config",
                "pricing",
                "set",
                model,
                "--input",
                "1",
                "--output",
                "2",
                "--cache-read",
                "0.25",
            ],
        )
        assert result.exit_code == 0, result.output

    source = repo / "calculator.py"
    source.write_text(
        '"""Small integer calculator."""\n\n'
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "def multiply(a: int, b: int) -> int:\n    return a * b\n"
    )
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Cost Smoke",
        "-c",
        "user.email=smoke@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    previous_cwd = Path.cwd()
    print(f"Smoke artifacts: {root}", flush=True)
    try:
        os.chdir(repo)
        base = [
            "generate",
            "--output",
            str(output),
            "--no-artifacts",
            "--max-tokens",
            "8192",
            "--request-limit",
            "24",
            "--instructions",
            "Keep documentation concise, under 300 words per page. Do not include Mermaid diagrams.",
        ]
        reports = {}
        for stage in ("generate", "update", "no-change"):
            if stage == "update":
                source.write_text(
                    source.read_text().replace("return a + b", "return max(-100, min(100, a + b))")
                )
                git(repo, "add", ".")
                git(
                    repo,
                    "-c",
                    "user.name=Cost Smoke",
                    "-c",
                    "user.email=smoke@example.invalid",
                    "commit",
                    "-qm",
                    "clamp addition",
                )
            flags = (
                [] if stage == "generate" else ["--update", "--tau-full", "1", "--tau-tree", "1"]
            )
            print(f"Running {stage}...", flush=True)
            result = runner.invoke(cli, base + flags)
            log = result.output
            for secret in (env["API_KEY"], env["BASE_URL"]):
                log = log.replace(secret, "[redacted]")
            (root / f"{stage}.log").write_text(log)
            if result.exit_code:
                raise RuntimeError(
                    f"{stage} failed with exit {result.exit_code}; see {root / (stage + '.log')}"
                )
            metadata = json.loads((output / "metadata.json").read_text())
            report = metadata["usage"]
            verify(report)
            verify_display(report, result.output)
            assert "By model" in result.output or report["n_calls"] == 0
            assert "By phase" in result.output or report["n_calls"] == 0
            if stage != "generate":
                record = json.loads((output / "update_record.json").read_text())
                assert record["usage_report"] == report == metadata["last_update"]["usage"]
                assert len(metadata["update_history"]) == (1 if stage == "update" else 2)
                assert record["outcome"] == ("incremental" if stage == "update" else "no_change")
            if stage == "no-change":
                assert report["n_calls"] == 0 and report["totals"]["cost_usd"] == 0
            else:
                assert report["n_calls"] > 0 and report["totals"]["cost_usd"] > 0
            reports[stage] = report
            (root / f"{stage}.json").write_text(json.dumps(report, indent=2))
            print(
                f"{stage}: {report['n_calls']} calls, ${report['totals']['cost_usd']:.8f} at synthetic test rates",
                flush=True,
            )
        (root / "reports.json").write_text(json.dumps(reports, indent=2))
    finally:
        os.chdir(previous_cwd)
        shutil.rmtree(config_dir)


if __name__ == "__main__":
    main()
