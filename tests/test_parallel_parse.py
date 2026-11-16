"""Parallel parsing must give exactly the sequential result, and the parse
timeout / worker count come from the environment."""

import ast
import inspect
import logging
import multiprocessing
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analysis import call_graph_analyzer as cga
from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import (
    CallGraphAnalyzer,
)

FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "from pkg.b import helper\nimport requests\n\ndef run():\n    return helper()\n",
    "pkg/b.py": "def helper():\n    return 1\n\nclass Thing:\n    def go(self):\n        return helper()\n",
    "pkg/c.py": "from pkg.a import run\n\ndef main():\n    run()\n",
    "pkg/d.py": "from pkg.b import Thing\n\ndef make():\n    return Thing().go()\n",
    "go.mod": "module example.com/demo\n\ngo 1.22\n",
    "geo/point.go": "package geo\n\nfunc Origin() int { return 0 }\n",
    "geo/move.go": "package geo\n\nfunc Move() int { return Origin() + 1 }\n",
    "app/main.go": 'package main\n\nimport "example.com/demo/geo"\n\nfunc main() { geo.Move() }\n',
    "pubspec.yaml": "name: demo\n",
    "lib/car.dart": "class Car {\n  void drive() { honk(); }\n  void honk() {}\n}\n",
    "lib/garage.dart": "import 'car.dart';\n\nclass Garage {\n  void open() { Car().drive(); }\n}\n",
    "lib/main.dart": "import 'package:demo/garage.dart';\n\nvoid main() { Garage().open(); }\n",
    "src/Geo/Point.lean": "namespace Geo\n\nstructure Point where\n  x : Nat\n\ndef origin : Point := ⟨0⟩\n\nprivate def secret := 1\n\nprotected def guarded := 2\n\nend Geo\n",
    "src/Geo/Use.lean": "import Geo.Point\n\nopen Geo\n\ndef useOrigin := origin\n",
    "web/util.ts": "export function add(a: number, b: number) { return a + b; }\n",
    "web/calc.ts": "import { add } from './util';\n\nexport class Calc {\n  sum() { return add(1, 2); }\n}\n",
    "web/index.ts": "import { Calc } from './calc';\n\nnew Calc().sum();\n",
}
LANG = {".py": "python", ".go": "go", ".dart": "dart", ".lean": "lean", ".ts": "typescript"}


def _repo(root: Path) -> list[dict]:
    code_files = []
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if path.suffix in LANG:
            code_files.append(
                {
                    "path": rel,
                    "name": path.name,
                    "extension": path.suffix,
                    "language": LANG[path.suffix],
                }
            )
    return code_files


def _run(root: Path, code_files: list[dict]) -> tuple[dict, CallGraphAnalyzer]:
    analyzer = CallGraphAnalyzer()
    return analyzer.analyze_code_files(code_files, str(root)), analyzer


def _state(analyzer: CallGraphAnalyzer) -> dict:
    return {
        "functions": list(analyzer.functions),
        "python_roots": sorted(analyzer._python_external_import_roots),
        "dart": sorted(analyzer._dart_directives),
        "go": {k: sorted(v) for k, v in analyzer._go_visible.items()},
        "lean_files": sorted(analyzer._lean_files),
        "lean_scopes": sorted(analyzer._lean_scopes),
        "lean_aliases": {k: list(v) for k, v in analyzer._lean_aliases.items()},
        "lean_private": sorted(analyzer._lean_private),
        "lean_protected": sorted(analyzer._lean_protected),
    }


def test_parallel_result_equals_sequential(tmp_path, monkeypatch):
    code_files = _repo(tmp_path)

    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "1")
    sequential, seq_analyzer = _run(tmp_path, code_files)

    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "4")

    def no_sequential(*args, **kwargs):
        raise AssertionError("parallel path not taken")

    monkeypatch.setattr(CallGraphAnalyzer, "_analyze_files_sequential", no_sequential)
    parallel, par_analyzer = _run(tmp_path, code_files)

    assert parallel == sequential
    assert _state(par_analyzer) == _state(seq_analyzer)

    # Guard against a vacuous pass: every language produced components, and
    # cross-file calls were resolved.
    paths = {f["relative_path"] for f in sequential["functions"]}
    for suffix in LANG:
        assert any(p.endswith(suffix) for p in paths), suffix
    assert sequential["call_graph"]["files_analyzed"] == len(code_files)
    assert len(sequential["relationships"]) >= 5
    assert "requests" in _state(seq_analyzer)["python_roots"]


def test_parse_timeout_from_env(monkeypatch):
    monkeypatch.delenv("CODEWIKI_PARSE_TIMEOUT", raising=False)
    assert cga.parse_timeout_seconds() == cga.DEFAULT_PARSE_TIMEOUT_S == 300
    monkeypatch.setenv("CODEWIKI_PARSE_TIMEOUT", "45")
    assert cga.parse_timeout_seconds() == 45
    for bad in ("0", "-3", "abc"):
        monkeypatch.setenv("CODEWIKI_PARSE_TIMEOUT", bad)
        with pytest.raises(ValueError):
            cga.parse_timeout_seconds()


@pytest.mark.parametrize("workers", ["1", "4"])
def test_invalid_timeout_fails_the_run_before_parsing(tmp_path, monkeypatch, workers):
    # Must not be swallowed per file (which used to leave an empty graph).
    code_files = _repo(tmp_path)
    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", workers)
    monkeypatch.setenv("CODEWIKI_PARSE_TIMEOUT", "abc")
    with pytest.raises(ValueError, match="CODEWIKI_PARSE_TIMEOUT"):
        _run(tmp_path, code_files)


def test_parse_worker_count(monkeypatch):
    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "3")
    assert cga.parse_worker_count(100) == 3
    assert cga.parse_worker_count(2) == 2
    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "0")
    with pytest.raises(ValueError):
        cga.parse_worker_count(100)

    monkeypatch.delenv("CODEWIKI_PARSE_WORKERS")
    monkeypatch.setattr(cga, "_cgroup_cpu_limit", lambda: None)
    assert cga.parse_worker_count(cga.FILES_PER_WORKER * 2 - 1) == 1
    assert 1 <= cga.parse_worker_count(10_000)
    monkeypatch.setattr(cga, "_cgroup_cpu_limit", lambda: 2)
    assert cga.parse_worker_count(10_000) <= 2
    monkeypatch.setattr(cga, "_cgroup_cpu_limit", lambda: None)
    monkeypatch.setattr(cga, "_available_memory_bytes", lambda: 3 * cga.WORKER_MEMORY_BYTES + 1)
    assert cga.parse_worker_count(10_000) <= 3
    monkeypatch.setattr(cga, "_available_memory_bytes", lambda: 0)
    assert cga.parse_worker_count(10_000) == 1

    # ProcessPoolExecutor rejects more than 61 workers on Windows.
    monkeypatch.setattr(cga, "_available_memory_bytes", lambda: None)
    monkeypatch.setattr(cga.os, "sched_getaffinity", lambda pid: set(range(128)), raising=False)
    monkeypatch.setattr(cga, "_is_windows", lambda: True)
    assert cga.parse_worker_count(10_000) == 61
    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "100")
    assert cga.parse_worker_count(10_000) == 61


def _fake_system(monkeypatch, tmp_path: Path, cgroup: str, files: dict[str, str]) -> None:
    root = tmp_path / "cgroup"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    root.mkdir(exist_ok=True)
    (tmp_path / "proc_cgroup").write_text(cgroup, encoding="utf-8")
    (tmp_path / "meminfo").write_text("MemTotal: 64000000 kB\nMemAvailable: 32000000 kB\n")
    monkeypatch.setattr(cga, "_CGROUP_ROOT", root)
    monkeypatch.setattr(cga, "_PROC_SELF_CGROUP", tmp_path / "proc_cgroup")
    monkeypatch.setattr(cga, "_PROC_MEMINFO", tmp_path / "meminfo")


def test_cgroup_v2_limits_at_an_ancestor(tmp_path, monkeypatch):
    _fake_system(
        monkeypatch,
        tmp_path,
        "garbage-line\n0::/kubepods/pod1/ctr\n",
        {
            "kubepods/cpu.max": "max 100000",
            "kubepods/pod1/cpu.max": "250000 100000",
            "kubepods/pod1/ctr/cpu.max": "max 100000",
            "kubepods/pod1/memory.max": str(2 * 1024**3),
            # 1.5 GiB in use, of which 0.5 GiB is reclaimable page cache.
            "kubepods/pod1/memory.current": str(3 * 1024**3 // 2),
            "kubepods/pod1/memory.stat": f"anon 1\ninactive_file {1024**3 // 2}\nmalformed\n",
            "kubepods/pod1/ctr/memory.max": "max",
            "kubepods/pod1/ctr/memory.current": "5",
        },
    )
    assert cga._cgroup_cpu_limit() == 3
    assert cga._available_memory_bytes() == 1024**3


def test_cgroup_v1_uses_own_path_and_ignores_unlimited(tmp_path, monkeypatch):
    _fake_system(
        monkeypatch,
        tmp_path,
        "4:memory:/system.slice/codewiki.service\n3:cpu,cpuacct:/system.slice/codewiki.service\n",
        {
            "cpu,cpuacct/cpu.cfs_quota_us": "-1",
            "cpu,cpuacct/cpu.cfs_period_us": "100000",
            "cpu,cpuacct/system.slice/codewiki.service/cpu.cfs_quota_us": "150000",
            "cpu,cpuacct/system.slice/codewiki.service/cpu.cfs_period_us": "100000",
            "memory/memory.limit_in_bytes": str(2**63 - 4096),
            "memory/memory.usage_in_bytes": "1",
        },
    )
    assert cga._cgroup_cpu_limit() == 2
    assert cga._available_memory_bytes() == 32_000_000 * 1024


def test_no_cgroup_information(tmp_path, monkeypatch):
    _fake_system(monkeypatch, tmp_path, "", {})
    assert cga._cgroup_cpu_limit() is None
    assert cga._available_memory_bytes() == 32_000_000 * 1024


def _add_slow_file(root: Path, code_files: list[dict], n: int) -> None:
    # Superlinear in the TypeScript analyzer: 12,000 functions take about 5 s.
    big = "".join(
        f"export function f{i}(x: number): number {{ return f{max(i - 1, 0)}(x) + f{(i * 7) % n}(x); }}\n"
        for i in range(n)
    )
    (root / "web" / "big.ts").write_text(big, encoding="utf-8")
    code_files.append(
        {"path": "web/big.ts", "name": "big.ts", "extension": ".ts", "language": "typescript"}
    )


def test_interrupt_stops_workers_without_waiting_for_files_in_flight(tmp_path, monkeypatch):
    code_files = _repo(tmp_path)
    _add_slow_file(tmp_path, code_files, 30_000)  # ~20 s, still parsing at the interrupt
    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "2")
    real_as_completed = cga.as_completed

    def interrupted(futures):
        for future in real_as_completed(futures):
            yield future
            raise KeyboardInterrupt  # Ctrl-C after the first finished file

    monkeypatch.setattr(cga, "as_completed", interrupted)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, code_files)
    assert time.monotonic() - started < 15  # well under the in-flight file's ~20 s
    assert multiprocessing.active_children() == []


def test_worker_timeout_drops_file_and_warning_reaches_parent(tmp_path, monkeypatch, caplog):
    code_files = _repo(tmp_path)
    _add_slow_file(tmp_path, code_files, 12_000)
    monkeypatch.setenv("CODEWIKI_PARSE_WORKERS", "2")
    monkeypatch.setenv("CODEWIKI_PARSE_TIMEOUT", "1")
    caplog.set_level(logging.WARNING)

    result, _ = _run(tmp_path, code_files)

    timeouts = [r for r in caplog.records if "Timeout analyzing" in r.getMessage()]
    assert len(timeouts) == 1 and "big.ts" in timeouts[0].getMessage()
    assert result["call_graph"]["files_analyzed"] == len(code_files) - 1
    paths = {f["relative_path"] for f in result["functions"]}
    assert not any(p.endswith("big.ts") for p in paths)
    assert any(p.endswith("calc.ts") for p in paths)


def _accumulators_written_by_analyzers() -> set[str]:
    """``self.<attr>`` that an ``_analyze_*_file`` method assigns, subscripts
    into, or mutates through a method call."""
    mutators = {"append", "extend", "update", "add", "setdefault", "insert", "pop", "clear"}
    written = set()
    for name, method in inspect.getmembers(CallGraphAnalyzer, inspect.isfunction):
        if not (name.startswith("_analyze_") and name.endswith("_file")):
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        for node in ast.walk(tree):
            target = None
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    while isinstance(t, ast.Subscript):
                        t = t.value
                    if isinstance(t, ast.Attribute):
                        target = t
                        if isinstance(t.value, ast.Name) and t.value.id == "self":
                            written.add(t.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in mutators
            ):
                target = node.func.value
                while isinstance(target, ast.Subscript):
                    target = target.value
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    written.add(target.attr)
    return written


CARRIED = {
    "functions",
    "call_relationships",
    "_python_external_import_roots",
    "_dart_directives",
    "_go_visible",
    "_lean_files",
    "_lean_scopes",
    "_lean_aliases",
    "_lean_private",
    "_lean_protected",
}


def test_analyzing_a_file_changes_only_carried_state(tmp_path):
    # Runtime counterpart of the static check below: also sees state written
    # by helpers that the _analyze_*_file methods call.
    code_files = _repo(tmp_path)
    modules = CallGraphAnalyzer._collect_python_modules(code_files)
    for file_info in code_files:
        fresh = CallGraphAnalyzer()
        fresh._python_project_modules = set(modules)
        analyzer = CallGraphAnalyzer()
        analyzer._python_project_modules = set(modules)
        assert analyzer._analyze_code_file(str(tmp_path), file_info, 60)
        changed = {k for k, v in vars(analyzer).items() if vars(fresh).get(k) != v}
        assert changed <= CARRIED, (file_info["path"], changed - CARRIED)


def test_every_analyzer_accumulator_is_carried_back_from_workers():
    # A worker's state outside _per_file_state is thrown away, so the parallel
    # path would silently lose an accumulator that is missing there.
    carried = CARRIED
    written = _accumulators_written_by_analyzers()
    assert {"functions", "call_relationships", "_lean_aliases"} <= written
    assert written <= carried, written - carried
    assert set(CallGraphAnalyzer()._per_file_state()) == {k.lstrip("_") for k in carried}


def test_record_buffer_strips_extras_and_caps_records(monkeypatch):
    import pickle

    monkeypatch.setattr(cga, "_MAX_RECORDS_PER_FILE", 3)
    buffer = cga._RecordBuffer()
    for i in range(5):
        buffer.put_nowait(
            logging.makeLogRecord({"name": "x", "msg": f"m{i}", "node": lambda: None})
        )
    records = buffer.take()
    pickle.dumps(records)  # an unpicklable ``extra`` must not sink the file's result
    assert [r.getMessage() for r in records[:3]] == ["m0", "m1", "m2"]
    assert "Dropped 2 further log records" in records[3].getMessage()
    assert buffer.take() == []
