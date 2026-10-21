#!/usr/bin/env python3
"""Real-model release check for CodeWiki, the same on Linux, Windows and macOS.

Installs nc-codewiki (from PyPI or a local wheel) into a fresh venv, configures it
from a key = value file, documents a git repo at <branch>~N, updates the docs at
<branch>, and asks questions through the MCP server. Logs and zipped docs land in
one output folder per run. Needs only Python 3.12+ and git; stdlib only.

    python release_check.py --repo https://github.com/you/project.git --config config.env

See README.md next to this file.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PACKAGE = "nc-codewiki"
QUESTION = "What does this project do and where does it start?"
LOG_PROBLEM = re.compile(r"Traceback|\bERROR\b|\bCRITICAL\b")
WINDOWS = os.name == "nt"

MCP_CLIENT = r"""
import asyncio, json, os, sys, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DOCS, QUESTION = sys.argv[1], sys.argv[2]
NEEDED = {"codewiki_ask", "codewiki_search", "codewiki_read", "codewiki_status"}
CALLS = [
    ("codewiki_status", {}),
    ("codewiki_search", {"query": "main entry point"}),
    ("codewiki_ask", {"question": QUESTION}),
]


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "codewiki", "mcp"], env=dict(os.environ)
    )
    failed = False
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            print("tools:", ", ".join(sorted(tools)), flush=True)
            if NEEDED - tools:
                print("FAIL: missing tools:", ", ".join(sorted(NEEDED - tools)), flush=True)
                failed = True
            for name, args in CALLS:
                print(f"\n--- {name} {json.dumps(args)}", flush=True)
                start = time.time()
                result = await session.call_tool(name, {**args, "docs_dir": DOCS})
                text = "\n".join(getattr(c, "text", "") for c in result.content)
                print(text, flush=True)
                ok = not result.isError and text.strip()
                print(f"--- {name}: {'ok' if ok else 'FAIL'} ({time.time() - start:.1f}s)", flush=True)
                failed |= not ok
    return 1 if failed else 0


sys.exit(asyncio.run(asyncio.wait_for(main(), timeout=900)))
"""


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------


def parse_config(path: Path) -> tuple[list[str], list[str], str]:
    """Return (config set args, extra generate args, api key) from a key = value file.

    Lines before any section, or under [config], go to `codewiki config set`;
    lines under [generate] are added to both generate runs. `key = true` becomes
    --key and `key = false` becomes --no-key (for --x/--no-x options).
    """
    sections: dict[str, list[str]] = {"config": [], "generate": []}
    current, api_key = "config", ""
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            if current not in sections:
                sys.exit(f"{path}:{n}: unknown section [{current}] (use [config] or [generate])")
            continue
        if "=" not in line:
            sys.exit(f"{path}:{n}: expected key = value, got: {raw}")
        key, value = (s.strip() for s in line.split("=", 1))
        key = key.removeprefix("--")
        value = value.strip("\"'")
        if not key or not value:
            continue  # an empty value means "leave the default"
        if value.lower() == "true":
            sections[current].append(f"--{key}")
        elif value.lower() == "false":
            sections[current].append(f"--no-{key}")
        else:
            sections[current] += [f"--{key}", value]
        if current == "config" and key == "api-key":
            api_key = value
    return sections["config"], sections["generate"], api_key


# ---------------------------------------------------------------------------
# Install source
# ---------------------------------------------------------------------------


def wheel_matches_here(name: str) -> bool:
    plat = {"win32": "win", "darwin": "macosx"}.get(sys.platform, "linux")
    tag = f"-cp{sys.version_info.major}{sys.version_info.minor}-"
    return tag in name and plat in name


def resolve_wheel(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_file():
        if path.suffix != ".whl":
            sys.exit(f"not a wheel: {path}")
        if not wheel_matches_here(path.name):
            print(
                f"warning: {path.name} may not match this OS / Python {platform.python_version()}"
            )
        return path
    if not path.is_dir():
        sys.exit(f"no such wheel file or folder: {path}")
    wheels = sorted(path.rglob("*.whl"))
    matching = [w for w in wheels if wheel_matches_here(w.name)] or wheels
    if not matching:
        sys.exit(f"no .whl files under {path}")
    if len(matching) == 1:
        return matching[0]
    for i, w in enumerate(matching, 1):
        print(f"  {i}) {w.relative_to(path)}")
    choice = ask(f"Which wheel? [1-{len(matching)}]: ")
    try:
        return matching[int(choice) - 1]
    except (ValueError, IndexError):
        sys.exit(f"invalid choice: {choice}")


def ask(prompt: str) -> str:
    if not sys.stdin.isatty():
        sys.exit("no install source given and stdin is not interactive; pass --package or --wheel")
    return input(prompt).strip()


def choose_install_source(args) -> str:
    if args.wheel:
        return str(resolve_wheel(Path(args.wheel)))
    if args.package:
        return args.package
    print("Install CodeWiki from:")
    print(f"  1) PyPI package ({PACKAGE})")
    print("  2) Local wheel file or folder (pre-publish check)")
    choice = ask("Choose [1/2]: ")
    if choice == "1":
        spec = ask(f"Package spec [{PACKAGE}] (e.g. {PACKAGE}==2.0.2): ")
        return spec or PACKAGE
    if choice == "2":
        return str(resolve_wheel(Path(ask("Path to .whl file or folder: "))))
    sys.exit(f"invalid choice: {choice}")


# ---------------------------------------------------------------------------
# Running steps
# ---------------------------------------------------------------------------


class Run:
    def __init__(self, out: Path, total: int, secret: str):
        self.out = out
        self.total = total
        self.secret = secret
        self.n = 0
        self.results: list[tuple[str, str, int | None, float, str]] = []

    def mask(self, text: str) -> str:
        return text.replace(self.secret, "****") if self.secret else text

    def step(
        self, title: str, log: Path, cmd: list[str], cwd: Path | None = None, env=None
    ) -> bool:
        """Run cmd, streaming its output to the console and appending it to log."""
        self.n += 1
        shown = self.mask(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        print(f"\n\033[1m[{self.n}/{self.total}] {title}\033[0m\n$ {shown}", flush=True)
        start = time.time()
        with log.open("a", encoding="utf-8") as f:
            f.write(
                f"\n===== [{self.n}/{self.total}] {title} ({datetime.now():%H:%M:%S})\n$ {shown}\n"
            )
            f.flush()
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except OSError as e:
                f.write(f"could not start: {e}\n")
                return self._done(title, False, None, start, log)
            # read1 returns whatever is available, so progress bars show live.
            while chunk := proc.stdout.read1(65536):
                text = self.mask(chunk.decode("utf-8", errors="replace"))
                sys.stdout.write(text)
                sys.stdout.flush()
                f.write(text)
            rc = proc.wait()
            f.write(f"===== exit code {rc}\n")
        return self._done(title, rc == 0, rc, start, log)

    def note(self, title: str, ok: bool, detail: str = "") -> bool:
        """Record a step that ran in this process (zipping, checks)."""
        self.n += 1
        print(f"\n\033[1m[{self.n}/{self.total}] {title}\033[0m {detail}", flush=True)
        return self._done(title, ok, None, time.time(), None)

    def _done(self, title, ok, rc, start, log) -> bool:
        took = time.time() - start
        mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
        print(f"--> {mark} {title} ({took:.0f}s)", flush=True)
        self.results.append((title, "PASS" if ok else "FAIL", rc, took, log.name if log else ""))
        return ok

    def skip(self, title: str) -> None:
        self.n += 1
        self.results.append((title, "SKIP", None, 0.0, ""))


def zip_docs(run: Run, docs: Path, name: str) -> None:
    title = f"Zip docs -> {name}.zip"
    if not docs.is_dir() or not any(docs.iterdir()):
        run.note(title, False, f"(no docs at {docs})")
        return
    archive = shutil.make_archive(str(run.out / name), "zip", root_dir=docs)
    pages = len(list(docs.rglob("*.md")))
    run.note(title, pages > 0, f"({pages} .md pages)")
    print(f"    {archive}")


def scan_logs(out: Path) -> list[str]:
    lines = []
    for log in sorted(out.glob("*.txt")):
        if log.name == "summary.txt":
            continue
        hits = [
            line.rstrip()
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if LOG_PROBLEM.search(line)
        ]
        lines.append(f"{log.name}: {len(hits)} Traceback/ERROR/CRITICAL lines")
        lines += [f"    {h[:200]}" for h in hits[:5]]
    return lines


def write_summary(run: Run, header: list[str]) -> bool:
    rows = [
        f"{status:<5} {took:>6.0f}s  rc={'' if rc is None else rc:<4} {title}"
        + (f"  [{log}]" if log else "")
        for title, status, rc, took, log in run.results
    ]
    text = "\n".join(header + ["", *rows, "", *scan_logs(run.out)]) + "\n"
    (run.out / "summary.txt").write_text(text, encoding="utf-8")
    print("\n" + "=" * 70 + "\n" + text + "=" * 70)
    return all(status != "FAIL" for _, status, *_ in run.results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", required=True, help="git URL (or local path) of the repo to document")
    p.add_argument("--config", required=True, type=Path, help="key = value config file")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--package", help=f"PyPI spec, e.g. {PACKAGE}==2.0.2")
    src.add_argument(
        "--wheel", help="local .whl file, or a folder of wheels (picks the one for this OS/Python)"
    )
    p.add_argument("--branch", default="main", help="branch to check out (default: main)")
    p.add_argument(
        "--behind", type=int, default=10, help="first generate at <branch>~N (default: 10)"
    )
    p.add_argument("--question", default=QUESTION, help="question for codewiki ask / MCP")
    p.add_argument(
        "--out", type=Path, help="output folder (default: ./release-check-<os>-py<ver>-<time>)"
    )
    args = p.parse_args()

    if not args.config.is_file():
        sys.exit(f"config file not found: {args.config}")
    if not shutil.which("git"):
        sys.exit("git is not on PATH")
    config_args, generate_args, api_key = parse_config(args.config)
    source = choose_install_source(args)

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    out = (
        args.out or Path(f"release-check-{sys.platform}-py{pyver}-{datetime.now():%Y%m%d-%H%M%S}")
    ).resolve()
    out.mkdir(parents=True, exist_ok=True)
    venv, home, repo = out / "venv", out / "home", out / "repo"
    home.mkdir(exist_ok=True)
    vpy = str(venv / ("Scripts/python.exe" if WINDOWS else "bin/python"))
    docs = repo / "docs"

    # Isolated home: `codewiki config set` must not touch your real ~/.codewiki or keyring.
    env = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        CODEWIKI_NO_KEYRING="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        CODEWIKI_DOCS_DIR=str(docs),
    )
    codewiki = [vpy, "-m", "codewiki"]
    setup, gen_log, upd_log, mcp_log = (
        out / n for n in ("setup-logs.txt", "logs.txt", "update-logs.txt", "mcp-logs.txt")
    )

    header = [
        f"CodeWiki release check  {datetime.now():%Y-%m-%d %H:%M}",
        f"OS:      {platform.platform()} ({platform.machine()})",
        f"Python:  {platform.python_version()} ({sys.executable})",
        f"Install: {source}",
        f"Repo:    {args.repo}  {args.branch}~{args.behind} -> {args.branch}",
        f"Output:  {out}",
    ]
    print("\n".join(header))
    run = Run(out, total=15, secret=api_key)

    def finish() -> int:
        ok = write_summary(run, header)
        print(f"Artifacts in {out}")
        return 0 if ok else 1

    # --- setup: stop at the first failure, nothing after it can work ---
    setup_steps = [
        ("Create venv", [sys.executable, "-m", "venv", str(venv)], None, None),
        ("Install uv", [vpy, "-m", "pip", "install", "--quiet", "--upgrade", "uv"], None, None),
        (
            f"Install {source}",
            [vpy, "-m", "uv", "pip", "install", "--python", vpy, source],
            None,
            None,
        ),
        ("codewiki --version", codewiki + ["--version"], None, env),
        ("codewiki config set", codewiki + ["config", "set", *config_args], None, env),
        ("codewiki config validate", codewiki + ["config", "validate", "--verbose"], None, env),
        ("git clone", ["git", "clone", args.repo, str(repo)], None, None),
        (
            f"git checkout {args.branch}~{args.behind}",
            ["git", "checkout", "--quiet", f"origin/{args.branch}~{args.behind}"],
            repo,
            None,
        ),
    ]
    if repo.exists():
        shutil.rmtree(repo)
    for title, cmd, cwd, step_env in setup_steps:
        if run.step(title, setup, cmd, cwd, step_env):
            continue
        if title.startswith("Install ") and title != "Install uv":
            # Record the uv failure (e.g. a corrupt uv cache entry; `uv cache clean <pkg>`
            # fixes that), but still run the rest of the check on a pip install.
            run.total += 1
            if run.step(f"{title} (pip fallback)", setup, [vpy, "-m", "pip", "install", source]):
                continue
        return finish()

    # --- generate at <branch>~N, then update at <branch>; keep going on failure ---
    gen = codewiki + ["generate", "--verbose", "--github-pages", *generate_args]
    run.step(f"codewiki generate at {args.branch}~{args.behind}", gen_log, gen, repo, env)
    zip_docs(run, docs, "docs")

    run.step(
        f"git checkout {args.branch}",
        upd_log,
        ["git", "checkout", "--quiet", "-B", args.branch, f"origin/{args.branch}"],
        repo,
        None,
    )
    run.step(f"codewiki generate --update at {args.branch}", upd_log, gen + ["--update"], repo, env)
    zip_docs(run, docs, "docs-updated")

    # --- ask: through MCP like an editor would, then the CLI for comparison ---
    if docs.is_dir():
        client = out / "mcp_client.py"
        client.write_text(MCP_CLIENT, encoding="utf-8")
        run.step(
            "MCP: codewiki_status / codewiki_search / codewiki_ask",
            mcp_log,
            [vpy, str(client), str(docs), args.question],
            repo,
            env,
        )
        run.step(
            "codewiki ask (CLI)",
            mcp_log,
            codewiki + ["ask", args.question, "--docs", str(docs)],
            repo,
            env,
        )
    else:
        run.skip("MCP ask (no docs)")
        run.skip("codewiki ask (no docs)")
    return finish()


if __name__ == "__main__":
    if WINDOWS:
        os.system("")  # turns on ANSI colours in the Windows console
    sys.exit(main())
