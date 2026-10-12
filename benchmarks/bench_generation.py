"""Benchmark `codewiki generate` end to end with different speed settings.

Each variant runs in a fresh output directory. With --seed, the variant starts
from the same clustered module tree (first_module_tree.json / module_tree.json
copied from an earlier run), so every variant documents the same modules and
only the scheduling / splitting settings differ.

    python benchmarks/bench_generation.py --repo ~/src/itsdangerous \\
        --out /tmp/bench --variant c1="--concurrency 1"
    python benchmarks/bench_generation.py --repo ~/src/itsdangerous --out /tmp/bench \\
        --seed /tmp/bench/c1 --variant c4="--concurrency 4" --variant c8="--concurrency 8"

Results are appended to <out>/results.jsonl and printed as a table.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

SEED_FILES = ("first_module_tree.json", "module_tree.json")
TS = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]")


def _agent_stats(log: str) -> dict:
    leaves = len(re.findall(r"Processing leaf module:", log))
    subs = len(re.findall(r"Generating documentation for sub-module:", log))
    parents = len(re.findall(r"Generating parent documentation for:", log))
    turns = [int(t) for t in re.findall(r"completed via caw \(turns=(\d+)", log)]
    return {
        "leaf_agents": leaves,
        "sub_agents": subs,
        "overview_calls": parents,
        "agent_runs": leaves + subs,
        "avg_turns": round(sum(turns) / len(turns), 1) if turns else None,
        "errors": len(re.findall(r"\bERROR\b", log)),
        # caw sleeps until a subscription limit resets; such a run's time is not comparable
        "limit_waits": len(re.findall(r"Usage limit reached", log)),
    }


def _pages(out: Path) -> dict:
    pages = [p for p in out.rglob("*.md") if "temp" not in p.parts]
    return {"pages": len(pages), "doc_kb": round(sum(p.stat().st_size for p in pages) / 1024, 1)}


def run_variant(name: str, args: str, repo: Path, out_root: Path, seed: Path | None) -> dict:
    out = out_root / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if seed is not None:
        for fname in SEED_FILES:
            src = seed / ("first_module_tree.json" if fname == "module_tree.json" else fname)
            shutil.copy(src, out / fname)
    cmd = [
        str(Path(sys.executable).with_name("codewiki")),
        "generate",
        "-o",
        str(out),
        "--verbose",
        *shlex.split(args),
    ]
    print(f"\n=== {name}: {' '.join(cmd)}", flush=True)
    started = time.time()
    with open(out_root / f"{name}.log", "w") as log_file:
        proc = subprocess.run(cmd, cwd=repo, stdout=log_file, stderr=subprocess.STDOUT)
    seconds = time.time() - started
    log = (out_root / f"{name}.log").read_text(errors="replace")
    result = {
        "variant": name,
        "args": args,
        "seeded": seed is not None,
        "exit_code": proc.returncode,
        "seconds": round(seconds, 1),
        **_agent_stats(log),
        **_pages(out),
    }
    with open(out_root / "results.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    print(json.dumps(result), flush=True)
    return result


def print_table(results: list[dict]) -> None:
    if not results:
        return
    base = results[0]["seconds"]
    cols = [
        "variant",
        "seconds",
        "speedup",
        "agent_runs",
        "avg_turns",
        "pages",
        "doc_kb",
        "errors",
        "limit_waits",
    ]
    print("\n" + " | ".join(cols))
    for r in results:
        row = {**r, "speedup": f"{base / r['seconds']:.2f}x" if r["seconds"] else "-"}
        print(" | ".join(str(row.get(c, "")) for c in cols))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=Path, help="output dir of an earlier run to reuse its tree")
    parser.add_argument(
        "--variant", action="append", required=True, help='name="extra generate args"'
    )
    ns = parser.parse_args()
    ns.out.mkdir(parents=True, exist_ok=True)
    results = []
    for spec in ns.variant:
        name, _, args = spec.partition("=")
        results.append(run_variant(name, args, ns.repo.resolve(), ns.out.resolve(), ns.seed))
    print_table(results)


if __name__ == "__main__":
    main()
