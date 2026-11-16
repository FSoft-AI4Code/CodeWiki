"""
Call Graph Analyzer

Central orchestrator for multi-language call graph analysis.
Coordinates language-specific analyzers to build comprehensive call graphs
across different programming languages in a repository.
"""

import logging
import logging.handlers
import math
import multiprocessing
import os
import re
import signal
import sys
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures import process as futures_process
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    CPP_STANDARD_HEADERS,
    is_external_symbol,
    is_macro_name,
)
from codewiki.src.be.dependency_analyzer.utils.grammars import ensure_pack_grammars
from codewiki.src.be.dependency_analyzer.utils.patterns import CODE_EXTENSIONS
from codewiki.src.be.dependency_analyzer.utils.security import safe_open_text

if TYPE_CHECKING:
    from codewiki.src.be.dependency_analyzer.analyzers.lean import LeanFile, LeanScope
    from codewiki.src.be.dependency_analyzer.analyzers.lean_resolution import LeanNameResolver

logger = logging.getLogger(__name__)


class TimeoutError(BaseException):
    """Raised when file parsing exceeds timeout.

    A ``BaseException`` on purpose: the language analyzers wrap many small
    steps in ``except Exception`` blocks.  If the alarm fired inside one of
    them, the error was swallowed, the alarm was already spent, and a slow
    file (e.g. a multi-megabyte vendored JS bundle) ran forever.  Only
    ``_analyze_code_file`` catches it.
    """


@contextmanager
def timeout(seconds):
    """Context manager for timeout on file parsing.

    SIGALRM only exists on Unix and can only be installed from the main
    thread.  Anywhere else (Windows, worker threads such as the MCP server's
    ``asyncio.to_thread`` pool) the body runs without a timeout instead of
    failing -- previously ``signal.signal`` raised from a worker thread and
    the per-file ``except Exception`` silently skipped every file.
    """
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def signal_handler(signum, frame):
        raise TimeoutError(f"File parsing exceeded {seconds}s timeout")

    old_handler = signal.signal(signal.SIGALRM, signal_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


# A 30 s limit dropped the central 6-8k line TypeScript files of real
# repositories (one took 91 s alone), leaving holes in the dependency graph.
# A dropped file costs documentation; a slow one only costs time.
DEFAULT_PARSE_TIMEOUT_S = 300
# Each worker re-imports the parent's ``__main__`` (about 1.5 s and 150 MB for
# the CLI), so a worker only pays off with several files to parse.
FILES_PER_WORKER = 8
# Memory to reserve per worker: the import plus parse trees (peak worker RSS
# was ~150 MB on a 678-file TypeScript repository), with headroom.
WORKER_MEMORY_BYTES = 400 * 1024 * 1024
# ProcessPoolExecutor rejects more workers than this on Windows.
_WINDOWS_MAX_WORKERS = getattr(futures_process, "_MAX_WINDOWS_WORKERS", 61)

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_PROC_MEMINFO = Path("/proc/meminfo")


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def parse_timeout_seconds() -> int:
    """Per-file parse timeout: ``CODEWIKI_PARSE_TIMEOUT`` or 300 s."""
    return _positive_int_env("CODEWIKI_PARSE_TIMEOUT", DEFAULT_PARSE_TIMEOUT_S)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _cgroup_dirs(v1_controller: str) -> list[Path]:
    """This process's cgroup directories, innermost first up to the mount
    root: the v2 hierarchy and the given v1 controller. In a container the
    listed path may be a host path; walking up then reaches the mount root,
    which is the container's own cgroup."""
    root = _CGROUP_ROOT
    dirs: list[Path] = []
    for line in (_read(_PROC_SELF_CGROUP) or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, path = parts
        if hierarchy == "0" and controllers == "":
            bases = [root]
        elif v1_controller in controllers.split(","):
            bases = [root / controllers, root / v1_controller]
        else:
            continue
        for base in bases:
            current = base / path.lstrip("/")
            while True:
                if current.is_dir() and current not in dirs:
                    dirs.append(current)
                if current == base or base not in current.parents:
                    break
                current = current.parent
    return dirs


def _cgroup_cpu_limit() -> int | None:
    """Tightest CPU quota over this process's cgroups (v2 ``cpu.max`` or v1
    CFS quota), rounded up; ``None`` when unlimited or unknown. Containers
    report every host CPU to ``sched_getaffinity`` even under ``--cpus``."""
    limits = []
    for directory in _cgroup_dirs("cpu"):
        fields = (_read(directory / "cpu.max") or "").split()
        quota = _read(directory / "cpu.cfs_quota_us")
        period = _read(directory / "cpu.cfs_period_us")
        candidates = []
        if len(fields) >= 2 and fields[0] != "max":
            candidates.append((fields[0], fields[1]))
        if quota is not None and period is not None:
            candidates.append((quota, period))
        for raw_quota, raw_period in candidates:
            try:
                q, p = int(raw_quota), int(raw_period)
            except ValueError:
                continue
            if q > 0 and p > 0:
                limits.append(math.ceil(q / p))
    return max(1, min(limits)) if limits else None


def _available_memory_bytes() -> int | None:
    """Memory this process can still use: ``MemAvailable``, capped by the
    remaining room of every limited cgroup (v2 or v1) it is in."""
    available = []
    for line in (_read(_PROC_MEMINFO) or "").splitlines():
        if line.startswith("MemAvailable:"):
            try:
                available.append(int(line.split()[1]) * 1024)
            except (IndexError, ValueError):
                pass
    for directory in _cgroup_dirs("memory"):
        limit = _read(directory / "memory.max") or _read(directory / "memory.limit_in_bytes")
        usage = _read(directory / "memory.current") or _read(directory / "memory.usage_in_bytes")
        try:
            limit_bytes, usage_bytes = int(limit or ""), int(usage or "")
        except ValueError:
            continue  # "max" (v2 unlimited) or unreadable
        if limit_bytes >= 1 << 60:  # v1 reports "unlimited" as ~2**63
            continue
        # Usage includes page cache the kernel reclaims on demand (a fresh
        # clone fills it); subtract it as docker stats and the kubelet do.
        stat = {}
        for line in (_read(directory / "memory.stat") or "").splitlines():
            fields = line.split()
            if len(fields) == 2:
                stat[fields[0]] = fields[1]
        # v1 ``inactive_file`` is this cgroup's own pages; ``total_`` covers the
        # subtree that ``usage_in_bytes`` counts. v2 has only ``inactive_file``.
        inactive = stat.get("total_inactive_file") or stat.get("inactive_file") or "0"
        try:
            usage_bytes -= min(usage_bytes, int(inactive))
        except ValueError:
            pass
        available.append(max(0, limit_bytes - usage_bytes))
    return min(available) if available else None


def parse_worker_count(file_count: int) -> int:
    """Parser processes for ``file_count`` files: ``CODEWIKI_PARSE_WORKERS``,
    else one per usable CPU (cgroup quota respected), per ``FILES_PER_WORKER``
    files and per ``WORKER_MEMORY_BYTES`` of available memory. 1 means the
    sequential path."""
    if os.environ.get("CODEWIKI_PARSE_WORKERS", "").strip():
        workers = min(_positive_int_env("CODEWIKI_PARSE_WORKERS", 1), file_count)
    else:
        if hasattr(os, "sched_getaffinity"):
            cpus = len(os.sched_getaffinity(0))
        else:
            cpus = os.cpu_count() or 1
        cpu_limit = _cgroup_cpu_limit()
        if cpu_limit is not None:
            cpus = min(cpus, cpu_limit)
        workers = min(cpus, file_count // FILES_PER_WORKER)
        memory = _available_memory_bytes()
        if memory is not None:
            workers = min(workers, memory // WORKER_MEMORY_BYTES)
    if _is_windows():
        workers = min(workers, _WINDOWS_MAX_WORKERS)
    return max(1, workers)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _logger_levels() -> dict[str, int]:
    """Every level set explicitly in this process, for workers to copy."""
    levels = {"": logging.getLogger().level}
    # dict.copy() is atomic under the GIL; another thread may add loggers.
    for name, candidate in logging.root.manager.loggerDict.copy().items():
        if isinstance(candidate, logging.Logger) and candidate.level != logging.NOTSET:
            levels[name] = candidate.level
    return levels


_STANDARD_RECORD_KEYS = frozenset(vars(logging.makeLogRecord({}))) | {"message"}
_MAX_RECORDS_PER_FILE = 10_000


class _RecordBuffer(list):
    """``QueueHandler`` target that keeps a worker's records for the current
    file. They go back to the parent with the file's result, so logging needs
    no pipe, lock or thread: a SIGALRM timeout or a killed worker cannot
    leave a half-written record behind or block the parent. Records lose
    their ``extra`` attributes, so an unpicklable one cannot sink the file's
    result, and a file keeps at most ``_MAX_RECORDS_PER_FILE`` of them."""

    dropped = 0

    def put_nowait(self, record: logging.LogRecord) -> None:
        if len(self) >= _MAX_RECORDS_PER_FILE:
            self.dropped += 1
            return
        self.append(
            logging.makeLogRecord(
                {k: v for k, v in vars(record).items() if k in _STANDARD_RECORD_KEYS}
            )
        )

    def take(self) -> list[logging.LogRecord]:
        records = list(self)
        if self.dropped:
            records.append(
                logging.makeLogRecord(
                    {
                        "name": __name__,
                        "levelno": logging.WARNING,
                        "levelname": "WARNING",
                        "msg": f"Dropped {self.dropped} further log records of this file",
                    }
                )
            )
        self.clear()
        self.dropped = 0
        return records


_worker_context: dict = {}


def _init_parse_worker(
    levels: dict[str, int], base_dir: str, python_project_modules: set, timeout_s: int
) -> None:
    # Ctrl-C reaches the whole foreground process group. A worker interrupted
    # inside the call queue's Queue.get dies holding its shared lock and the
    # other workers block on it forever, so only the parent handles SIGINT.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    records = _RecordBuffer()
    root = logging.getLogger()
    # QueueHandler.prepare() turns records into picklable ones.
    root.handlers[:] = [logging.handlers.QueueHandler(records)]
    for name, level in levels.items():
        logging.getLogger(name or None).setLevel(level)
    # Handlers live in the parent; let every record reach the buffer.
    for candidate in logging.root.manager.loggerDict.values():
        if isinstance(candidate, logging.Logger):
            candidate.handlers.clear()
            candidate.propagate = True
    _worker_context["records"] = records
    _worker_context["base_dir"] = base_dir
    _worker_context["python_project_modules"] = python_project_modules
    _worker_context["timeout_s"] = timeout_s


def _analyze_file_in_worker(file_info: dict) -> tuple[bool, dict, list[logging.LogRecord]]:
    records = _worker_context["records"]
    records.take()
    analyzer = CallGraphAnalyzer()
    analyzer._python_project_modules = _worker_context["python_project_modules"]
    completed = analyzer._analyze_code_file(
        _worker_context["base_dir"], file_info, _worker_context["timeout_s"]
    )
    return completed, analyzer._per_file_state(), records.take()


def _replay_worker_records(records: list[logging.LogRecord]) -> None:
    """Hand worker records to the parent's logger of the same name, so they go
    through the levels and handlers the caller configured."""
    for record in records:
        try:
            target = logging.getLogger(record.name)
            if target.isEnabledFor(record.levelno):
                target.handle(record)
        except Exception as e:  # noqa: BLE001 — logging must not change a file's outcome
            logger.warning(f"Could not replay a worker log record: {e!s}")


class CallGraphAnalyzer:
    def __init__(self):
        """Initialize the call graph analyzer."""
        self.functions: dict[str, Node] = {}
        self._dart_directives: dict[str, list] = {}
        self._dart_visible: dict[str, set[str]] = {}
        self._dart_library_members: dict[str, set[str]] = {}
        self._dart_unscoped = False
        self._go_visible: dict[str, set[str]] = {}
        self._go_index = None
        self._reset_lean_state()
        self.call_relationships: list[CallRelationship] = []
        self._python_project_modules: set = set()
        self._python_external_import_roots: set = set()
        logger.debug("CallGraphAnalyzer initialized.")

    def analyze_code_files(self, code_files: list[dict], base_dir: str) -> dict:
        """
        Complete analysis: Analyze all files to build complete call graph with all nodes.

        This approach:
        1. Analyzes all code files
        2. Extracts all functions and relationships
        3. Builds complete call graph
        4. Returns all nodes and relationships
        """
        logger.debug(f"Starting analysis of {len(code_files)} files")
        logger.info(f"📊 Parsing {len(code_files)} source files (this may take a few minutes)...")

        self.functions = {}
        self.call_relationships = []
        code_files = self._route_contextual_headers(code_files, base_dir)
        # Fail once, with a clear message, when a pack grammar cannot be
        # downloaded, instead of once per file inside the per-file handlers.
        ensure_pack_grammars(f["language"] for f in code_files)
        self._python_project_modules = self._collect_python_modules(code_files)
        self._python_external_import_roots = set()
        self._dart_directives = {}
        self._dart_visible = {}
        self._dart_library_members = {}
        self._dart_unscoped = False
        self._go_visible = {}
        self._go_index = None
        self._reset_lean_state()

        # Read both settings before any parsing: a bad value must fail the
        # run, not be swallowed by the per-file error handling.
        timeout_s = parse_timeout_seconds()
        workers = parse_worker_count(len(code_files))
        start_time = time.time()
        if workers > 1:
            files_analyzed, files_failed = self._analyze_files_parallel(
                code_files, base_dir, workers, timeout_s
            )
        else:
            files_analyzed, files_failed = self._analyze_files_sequential(
                code_files, base_dir, timeout_s
            )

        elapsed_time = time.time() - start_time
        logger.info(
            f"✓ Analysis complete: {files_analyzed}/{len(code_files)} files analyzed, "
            f"{files_failed} failed, {len(self.functions)} functions, {len(self.call_relationships)} relationships ({elapsed_time:.1f}s)"
        )

        if self._dart_directives:
            self._build_dart_scopes(base_dir)

        logger.debug("Resolving call relationships")
        self._resolve_call_relationships()
        self._deduplicate_relationships()
        viz_data = self._generate_visualization_data()

        return {
            "call_graph": {
                "total_functions": len(self.functions),
                "total_calls": len(self.call_relationships),
                "languages_found": list({f.get("language") for f in code_files}),
                "files_analyzed": files_analyzed,
                "analysis_approach": "complete_unlimited",
            },
            "functions": [func.model_dump() for func in self.functions.values()],
            "relationships": [rel.model_dump() for rel in self.call_relationships],
            "visualization": viz_data,
        }

    def _analyze_files_sequential(
        self, code_files: list[dict], base_dir: str, timeout_s: int
    ) -> tuple[int, int]:
        files_analyzed = 0
        files_failed = 0
        start_time = time.time()

        for idx, file_info in enumerate(code_files, 1):
            file_path = file_info["path"]
            # Log progress every file with elapsed time
            if idx % max(1, len(code_files) // 10) == 0 or idx <= 5:
                elapsed = time.time() - start_time
                rate = idx / elapsed if elapsed > 0 else 0
                remaining = (len(code_files) - idx) / rate if rate > 0 else 0
                logger.info(
                    f"  [{idx}/{len(code_files)}] {file_path} ({elapsed:.1f}s elapsed, ~{remaining:.1f}s remaining)"
                )

            # _analyze_code_file handles every per-file error itself.
            if self._analyze_code_file(base_dir, file_info, timeout_s):
                files_analyzed += 1
            else:
                files_failed += 1
        return files_analyzed, files_failed

    def _analyze_files_parallel(
        self, code_files: list[dict], base_dir: str, workers: int, timeout_s: int
    ) -> tuple[int, int]:
        """Parse files in worker processes, then merge per-file state in the
        original file order, so the result equals the sequential sweep.

        Per-file analysis only adds to the accumulators of ``_per_file_state``
        and never reads another file's results, which is what makes this
        safe. ``spawn`` because callers may hold threads (fork is unsafe then)
        and each worker runs tasks on its main thread, where SIGALRM works.
        """
        total = len(code_files)
        logger.info(f"  Parsing with {workers} worker processes, {timeout_s}s timeout per file")

        def size(file_info: dict) -> int:
            try:
                return (Path(base_dir) / file_info["path"]).stat().st_size
            except OSError:
                return 0

        # Largest first, so the slow files do not start last and set the wall time.
        schedule = sorted(range(total), key=lambda i: size(code_files[i]), reverse=True)
        results: list[dict | None] = [None] * total
        failed: set[int] = set()

        ctx = multiprocessing.get_context("spawn")
        start_time = time.time()
        pool = None
        try:
            pool = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=ctx,
                initializer=_init_parse_worker,
                initargs=(
                    _logger_levels(),
                    base_dir,
                    self._python_project_modules,
                    timeout_s,
                ),
            )
            futures = {pool.submit(_analyze_file_in_worker, code_files[i]): i for i in schedule}
            for done, future in enumerate(as_completed(futures), 1):
                i = futures[future]
                file_path = code_files[i]["path"]
                try:
                    completed, results[i], records = future.result()
                    _replay_worker_records(records)
                    if not completed:
                        failed.add(i)
                except BrokenProcessPool as e:
                    # A worker died or could not start. Its results are lost,
                    # so the graph cannot be completed: fail loudly.
                    raise RuntimeError(
                        f"A parser worker process died or failed to start ({workers} "
                        f"workers). Likely causes: killed for memory, or a calling script "
                        f"without an `if __name__ == '__main__':` guard (worker processes "
                        f"are spawned and re-import it). Retry with fewer workers, e.g. "
                        f"CODEWIKI_PARSE_WORKERS=1 for the sequential path."
                    ) from e
                except Exception as e:  # noqa: BLE001 — one unparsable file must not abort the sweep
                    failed.add(i)
                    logger.warning(
                        f"⚠️  Failed to analyze {Path(base_dir) / file_path}: {str(e)[:200]}"
                    )
                if done % max(1, total // 10) == 0 or done <= 5:
                    logger.info(
                        f"  [{done}/{total}] {file_path} ({time.time() - start_time:.1f}s elapsed)"
                    )
        except BaseException:
            # Ctrl-C or a fatal error: drop the queued files and stop the
            # workers now instead of waiting for the files in flight (each up
            # to the parse timeout). ``_processes`` is private but present in
            # every CPython 3.12-3.14 (3.14 wraps it as terminate_workers()).
            for process in list((getattr(pool, "_processes", None) or {}).values()):
                process.terminate()
            if pool is not None:
                # The pool's manager thread sees the dead workers and reaps
                # them; joining them here as well would race its waitpid.
                pool.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

        for state in results:
            if state is not None:
                self._merge_per_file_state(state)
        return total - len(failed), len(failed)

    def _per_file_state(self) -> dict:
        """Everything ``_analyze_code_file`` accumulates, for a worker to return."""
        return {
            "functions": self.functions,
            "call_relationships": self.call_relationships,
            "python_external_import_roots": self._python_external_import_roots,
            "dart_directives": self._dart_directives,
            "go_visible": self._go_visible,
            "lean_files": self._lean_files,
            "lean_scopes": self._lean_scopes,
            "lean_aliases": dict(self._lean_aliases),
            "lean_private": self._lean_private,
            "lean_protected": self._lean_protected,
        }

    def _merge_per_file_state(self, state: dict) -> None:
        # dict.update keeps a key's first position and takes the last value:
        # the same as the sequential per-key assignments.
        self.functions.update(state["functions"])
        self.call_relationships.extend(state["call_relationships"])
        self._python_external_import_roots.update(state["python_external_import_roots"])
        self._dart_directives.update(state["dart_directives"])
        self._go_visible.update(state["go_visible"])
        self._lean_files.update(state["lean_files"])
        self._lean_scopes.update(state["lean_scopes"])
        for alias, node_ids in state["lean_aliases"].items():
            self._lean_aliases[alias].extend(node_ids)
        self._lean_private.update(state["lean_private"])
        self._lean_protected.update(state["lean_protected"])

    def extract_code_files(self, file_tree: dict) -> list[dict]:
        """
        Extract code files from file tree structure.

        Filters files based on supported extensions and excludes test/config files.

        Args:
            file_tree: Nested dictionary representing file structure

        Returns:
            List of code file information dictionaries
        """
        code_files = []

        def traverse(tree):
            if tree["type"] == "file":
                ext = tree.get("extension", "").lower()
                if ext in CODE_EXTENSIONS:
                    name = tree["name"].lower()
                    if not any(skip in name for skip in []):
                        code_files.append(
                            {
                                "path": tree["path"],
                                "name": tree["name"],
                                "extension": ext,
                                "language": CODE_EXTENSIONS[ext],
                            }
                        )
            elif tree["type"] == "directory" and tree.get("children"):
                for child in tree["children"]:
                    traverse(child)

        traverse(file_tree)
        return code_files

    def _route_contextual_headers(self, code_files: list[dict], base_dir: str) -> list[dict]:
        """Route ambiguous .h headers per file.

        A header is parsed as C++ when its own content shows C++ signals, or
        when the repository is C++-only (so even a signal-free header cannot be
        C). In a mixed C/C++ repository, a plain C header stays routed as C.
        """
        cpp_extensions = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hxx", ".h++"}
        has_cpp_files = any(
            file_info.get("extension", "").lower() in cpp_extensions
            or file_info.get("language") == "cpp"
            for file_info in code_files
        )
        has_c_files = any(
            file_info.get("extension", "").lower() == ".c" for file_info in code_files
        )

        routed_files = []
        for file_info in code_files:
            routed = dict(file_info)
            if routed.get("extension", "").lower() == ".h" and (
                self._header_has_cpp_signal(base_dir, routed["path"])
                or has_cpp_files
                and not has_c_files
            ):
                routed["language"] = "cpp"
            routed_files.append(routed)
        return routed_files

    def _header_has_cpp_signal(self, base_dir: str, relative_path: str) -> bool:
        base = Path(base_dir)
        try:
            content = safe_open_text(base, base / relative_path)
        except Exception:  # noqa: BLE001 — unreadable file simply is not an entry point
            return False

        if re.search(
            r"\b(?:namespace\s+[A-Za-z_{:]|class\s+[A-Za-z_]|template\s*<"
            r"|typename\b|(?:public|private|protected)\s*:)",
            content,
        ):
            return True
        if "::" in content:
            return True
        for header in CPP_STANDARD_HEADERS:
            if f"#include <{header}>" in content:
                return True
        return False

    def _analyze_code_file(self, repo_dir: str, file_info: dict, timeout_s: int) -> bool:
        """
        Analyze a single code file based on its language.

        Routes to appropriate language-specific analyzer.

        Args:
            repo_dir: Repository directory path
            file_info: File information dictionary
            timeout_s: Per-file timeout in seconds

        Returns:
            False when the file timed out or its analysis raised.
        """

        file_path = file_info.get("path")
        try:
            # Inside the try: callers rely on every per-file error being
            # handled here.
            base = Path(repo_dir)
            file_path = base / file_info["path"]
            with timeout(timeout_s):
                content = safe_open_text(base, file_path)
                language = file_info["language"]
                if language == "python":
                    self._analyze_python_file(file_path, content, repo_dir)
                elif language == "javascript":
                    self._analyze_javascript_file(file_path, content, repo_dir)
                elif language == "typescript":
                    self._analyze_typescript_file(file_path, content, repo_dir)
                elif language == "java":
                    self._analyze_java_file(file_path, content, repo_dir)
                elif language == "kotlin":
                    self._analyze_kotlin_file(file_path, content, repo_dir)
                elif language == "csharp":
                    self._analyze_csharp_file(file_path, content, repo_dir)
                elif language == "c":
                    self._analyze_c_file(file_path, content, repo_dir)
                elif language == "cpp":
                    self._analyze_cpp_file(file_path, content, repo_dir)
                elif language == "php":
                    self._analyze_php_file(file_path, content, repo_dir)
                elif language == "ruby":
                    self._analyze_ruby_file(file_path, content, repo_dir)
                elif language == "scala":
                    self._analyze_scala_file(file_path, content, repo_dir)
                elif language == "rust":
                    self._analyze_rust_file(file_path, content, repo_dir)
                elif language == "dart":
                    self._analyze_dart_file(file_path, content, repo_dir)
                elif language == "go":
                    self._analyze_go_file(file_path, content, repo_dir)
                elif language == "zig":
                    self._analyze_zig_file(file_path, content, repo_dir)
                elif language == "lean":
                    self._analyze_lean_file(file_path, content, repo_dir)
                elif language == "vue":
                    self._analyze_vue_file(file_path, content, repo_dir)
                # else:
                #     logger.warning(
                #         f"Unsupported language for call graph analysis: {language} for file {file_path}"
                #     )

        except TimeoutError as e:
            logger.warning(f"⏱️  Timeout analyzing {file_path}: {e!s}")
            return False
        except Exception as e:  # noqa: BLE001 — per-file analysis is best-effort
            logger.warning(f"⚠️  Failed to analyze {file_path}: {str(e)[:200]}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return False
        return True

    def _analyze_python_file(self, file_path: str, content: str, base_dir: str):
        """
        Analyze Python file using Python AST analyzer.

        Args:
            file_path: Relative path to the Python file
            content: File content string
            base_dir: Repository base directory path
        """
        from codewiki.src.be.dependency_analyzer.analyzers.python import analyze_python_file

        try:
            functions, relationships, external_import_roots = analyze_python_file(
                file_path,
                content,
                repo_path=base_dir,
                project_modules=self._python_project_modules,
            )

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
            self._python_external_import_roots.update(external_import_roots)
        except Exception:
            logger.exception(f"Failed to analyze Python file {file_path}")

    @staticmethod
    def _collect_python_modules(code_files: list[dict]) -> set:
        """Dotted module paths for every Python file in the repository."""
        modules = set()
        for file_info in code_files:
            if file_info.get("language") != "python":
                continue
            path = file_info["path"]
            for ext in (".py", ".pyx"):
                if path.endswith(ext):
                    path = path[: -len(ext)]
                    break
            module = path.replace("/", ".").replace("\\", ".")
            module = module.removesuffix(".__init__")
            if module:
                modules.add(module)
        return modules

    def _analyze_javascript_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze JavaScript file using tree-sitter based AST analyzer

        Args:
            file_path: Relative path to the JavaScript file
            content: File content string
            repo_dir: Repository base directory
        """
        try:
            from codewiki.src.be.dependency_analyzer.analyzers.javascript import (
                analyze_javascript_file_treesitter,
            )

            functions, relationships = analyze_javascript_file_treesitter(
                file_path, content, repo_path=repo_dir
            )

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)

        except Exception:
            logger.exception(f"Failed to analyze JavaScript file {file_path}")

    def _analyze_typescript_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze TypeScript file using tree-sitter based AST analyzer

        Args:
            file_path: Relative path to the TypeScript file
            content: File content string
        """
        try:
            from codewiki.src.be.dependency_analyzer.analyzers.typescript import (
                analyze_typescript_file_treesitter,
            )

            functions, relationships = analyze_typescript_file_treesitter(
                file_path, content, repo_path=repo_dir
            )

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)

        except Exception:
            logger.exception(f"Failed to analyze TypeScript file {file_path}")

    def _analyze_c_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze C file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the C file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.c import analyze_c_file

        functions, relationships = analyze_c_file(file_path, content, repo_path=repo_dir)

        for func in functions:
            func_id = func.id if func.id else f"{file_path}:{func.name}"
            self.functions[func_id] = func

        self.call_relationships.extend(relationships)

    def _analyze_cpp_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze C++ file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the C++ file
            content: File content string
        """
        from codewiki.src.be.dependency_analyzer.analyzers.cpp import analyze_cpp_file

        functions, relationships = analyze_cpp_file(file_path, content, repo_path=repo_dir)

        for func in functions:
            func_id = func.id if func.id else f"{file_path}:{func.name}"
            self.functions[func_id] = func

        self.call_relationships.extend(relationships)

    def _analyze_java_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Java file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Java file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.java import analyze_java_file

        try:
            functions, relationships = analyze_java_file(file_path, content, repo_path=repo_dir)
            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Java file {file_path}")

    def _analyze_kotlin_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Kotlin file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Kotlin file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.kotlin import analyze_kotlin_file

        try:
            functions, relationships = analyze_kotlin_file(file_path, content, repo_path=repo_dir)
            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Kotlin file {file_path}")

    def _analyze_csharp_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze C# file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the C# file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.csharp import analyze_csharp_file

        try:
            functions, relationships = analyze_csharp_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze C# file {file_path}")

    def _analyze_php_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze PHP file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the PHP file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.php import analyze_php_file

        try:
            functions, relationships = analyze_php_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze PHP file {file_path}")

    def _analyze_ruby_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Ruby file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Ruby file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.ruby import analyze_ruby_file

        try:
            functions, relationships = analyze_ruby_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Ruby file {file_path}")

    def _analyze_scala_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Scala file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Scala file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.scala import analyze_scala_file

        try:
            functions, relationships = analyze_scala_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Scala file {file_path}")

    def _analyze_rust_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Rust file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Rust file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.rust import analyze_rust_file

        try:
            functions, relationships = analyze_rust_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Rust file {file_path}")

    def _analyze_dart_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Dart file using tree-sitter based analyzer.

        Besides components and relationships, records the file's
        import/export/part directives for Dart scope-aware resolution.

        Args:
            file_path: Path to the Dart file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.dart import TreeSitterDartAnalyzer

        try:
            analyzer = TreeSitterDartAnalyzer(str(file_path), content, repo_path=repo_dir)

            for func in analyzer.nodes:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(analyzer.call_relationships)
            self._dart_directives[analyzer.relative_path] = analyzer.directives
        except Exception:
            logger.exception(f"Failed to analyze Dart file {file_path}")

    def _analyze_zig_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Zig file using tree-sitter based analyzer.

        Args:
            file_path: Path to the Zig file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.zig import analyze_zig_file

        try:
            functions, relationships = analyze_zig_file(str(file_path), content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Zig file {file_path}")

    def _analyze_vue_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze a Vue single-file component.

        The <script> blocks go through the JavaScript/TypeScript analyzers; the
        component itself, its options-API members, and its template usage are
        added by the Vue analyzer.

        Args:
            file_path: Path to the .vue file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.vue import analyze_vue_file

        try:
            functions, relationships = analyze_vue_file(str(file_path), content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Vue file {file_path}")

    def _reset_lean_state(self) -> None:
        self._lean_files: dict[str, LeanFile] = {}
        self._lean_scopes: dict[str, LeanScope] = {}
        self._lean_aliases: dict[str, list[str]] = defaultdict(list)
        self._lean_private: set[str] = set()
        self._lean_protected: set[str] = set()
        self._lean_resolver: LeanNameResolver | None = None

    def _analyze_lean_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze a Lean 4 file.

        Besides components and references, records the file's imports, each
        declaration's namespaces and open namespaces, and the constructor and
        field names of its structures, for Lean resolution by exact name.

        Args:
            file_path: Path to the Lean file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.lean import TreeSitterLeanAnalyzer

        try:
            analyzer = TreeSitterLeanAnalyzer(str(file_path), content, repo_path=repo_dir)

            for func in analyzer.nodes:
                self.functions[func.id] = func

            self.call_relationships.extend(analyzer.call_relationships)
            self._lean_files[analyzer.relative_path] = analyzer.scan
            self._lean_scopes.update(analyzer.scopes)
            for alias, node_id in analyzer.aliases.items():
                self._lean_aliases[alias].append(node_id)
            self._lean_private.update(analyzer.private_ids)
            self._lean_protected.update(analyzer.protected_ids)
        except Exception:
            logger.exception(f"Failed to analyze Lean file {file_path}")

    def _resolve_lean_callee(self, callee: str, caller: Node) -> str | None:
        from codewiki.src.be.dependency_analyzer.analyzers.lean_resolution import LeanNameResolver

        if self._lean_resolver is None:
            self._lean_resolver = LeanNameResolver(
                self.functions,
                self._lean_files,
                self._lean_scopes,
                self._lean_aliases,
                self._lean_private,
                self._lean_protected,
            )
        return self._lean_resolver.resolve(callee, caller)

    def _analyze_go_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Go file using tree-sitter based analyzer.

        Besides components and relationships, records the package directories
        the file can see (its own and the repository packages it imports) for
        Go package-scoped resolution.

        Args:
            file_path: Path to the Go file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.go import TreeSitterGoAnalyzer

        try:
            analyzer = TreeSitterGoAnalyzer(str(file_path), content, repo_path=repo_dir)

            for func in analyzer.nodes:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(analyzer.call_relationships)
            self._go_visible[analyzer.relative_path] = analyzer.visible_dirs
        except Exception:
            logger.exception(f"Failed to analyze Go file {file_path}")

    def _resolve_go_callee(self, callee: str, caller: Node) -> str | None:
        """Go names resolve inside the package they name (a directory), or,
        for a method on a value of unknown type, among the packages the
        caller's file can see."""
        from codewiki.src.be.dependency_analyzer.analyzers.go import (
            GoPackageIndex,
            resolve_go_callee,
        )

        if self._go_index is None:
            self._go_index = GoPackageIndex(self.functions)
        caller_file = Path(caller.relative_path).as_posix()
        return resolve_go_callee(callee, caller, self._go_index, self._go_visible.get(caller_file))

    def _build_dart_scopes(self, base_dir: str) -> None:
        """Resolve recorded Dart directives into per-file visibility scopes."""
        from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
            DartPackageResolver,
            build_scopes,
        )

        files = set(self._dart_directives)
        resolver = DartPackageResolver.from_files(base_dir, files)
        scopes = build_scopes(self._dart_directives, resolver, files)
        self._dart_library_members = scopes.library_members
        if resolver.packages:
            self._dart_visible = scopes.visible
            return
        # Without a pubspec.yaml (e.g. only `lib/` was analyzed) `package:`
        # imports resolve to nothing, and scoping would drop nearly every
        # cross-file edge. Match names across all Dart files instead.
        self._dart_unscoped = True
        logger.warning(
            "No pubspec.yaml found for the analyzed Dart files; `package:` imports "
            "cannot be resolved, so Dart calls are matched by name across Dart files."
        )

    def _dart_scope_candidates(
        self, func_ids: list[str], scope: set[str] | None, caller_file: str | None = None
    ) -> list[str]:
        from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
            is_private_dart_name,
        )

        own_library = self._dart_library_members.get(caller_file, set()) if caller_file else set()
        candidates: list[str] = []
        for func_id in func_ids:
            func = self.functions.get(func_id)
            if func_id in candidates or func is None:
                continue
            func_file = Path(func.relative_path).as_posix()
            if scope is not None and func_file not in scope:
                continue
            # A `_`-prefixed segment in the target (`_Ticker.tick`) is private
            # to its library, whatever the callee's own spelling.
            if is_private_dart_name(func.name) and func_file not in own_library:
                continue
            candidates.append(func_id)
        return candidates

    def _resolve_dart_callee(
        self, callee: str, caller: Node, lang_indexes: dict | None
    ) -> str | None:
        """Prefer definitions the caller's library can see (imports, parts,
        re-exports); library-private names only resolve inside the library."""
        from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
            is_private_dart_name,
        )

        if not lang_indexes or "::" in callee:
            return None
        caller_file = Path(caller.relative_path).as_posix()
        scope: set[str] | None
        if is_private_dart_name(callee):
            scope = self._dart_library_members.get(caller_file, {caller_file})
        elif self._dart_unscoped:
            scope = None  # any Dart file
        else:
            scope = self._dart_visible.get(caller_file)
            if not scope:
                return None
        keys = [callee]
        if "." in callee:
            keys.append(callee.split(".")[0])
        for key in keys:
            # Exact names first: an imported `helper()` must not turn ambiguous
            # because some visible class also has a `helper` method.
            for index in ("exact", "simple"):
                candidates = self._dart_scope_candidates(
                    lang_indexes[index].get(key, []), scope, caller_file
                )
                if len(candidates) == 1:
                    return candidates[0]
                if candidates:
                    break

        from codewiki.src.be.dependency_analyzer.analyzers.dart_flutter import (
            RIVERPOD_NOTIFIER_TYPE,
            RIVERPOD_PROVIDER_TYPE,
            provider_alias_candidates,
        )

        # riverpod_generator providers live in ignored *.g.dart files: map
        # `fooProvider` to the @riverpod declaration it was generated from.
        for alias in provider_alias_candidates(callee):
            candidates = [
                func_id
                for func_id in self._dart_scope_candidates(
                    lang_indexes["exact"].get(alias, []), scope, caller_file
                )
                if self.functions[func_id].node_type
                in (RIVERPOD_PROVIDER_TYPE, RIVERPOD_NOTIFIER_TYPE)
                and self.functions[func_id].name == alias
            ]
            if len(candidates) == 1:
                return candidates[0]
        return None

    def _resolve_call_relationships(self):
        """
        Resolve function call relationships across all languages.

        Attempts to match function calls to actual function definitions,
        handling cross-language calls where possible.
        """
        for func_info in self.functions.values():
            if not func_info.language:
                file_ext = Path(func_info.file_path).suffix.lower()
                func_info.language = CODE_EXTENSIONS.get(file_ext)

        indexes = self._build_resolution_indexes()
        self._go_index = None

        resolved_count = 0
        for relationship in self.call_relationships:
            if relationship.is_resolved and relationship.callee in self.functions:
                continue

            resolved_id = self._resolve_callee(relationship, indexes)
            if resolved_id:
                relationship.callee = resolved_id
                relationship.is_resolved = True
                resolved_count += 1

        dotted_packages = self._dotted_project_packages()
        self.call_relationships = [
            relationship
            for relationship in self.call_relationships
            if relationship.is_resolved
            or not self._has_authoritative_resolution(relationship)
            and not self._is_external_callee(
                self._caller_language(relationship.caller),
                relationship.callee,
                dotted_packages,
            )
        ]

        if any(func.language == "go" for func in self.functions.values()):
            from codewiki.src.be.dependency_analyzer.analyzers.go import link_go_interfaces

            # Go types implement interfaces implicitly: link them once every
            # method of the package is known.
            self.call_relationships.extend(link_go_interfaces(self.functions))

    def _has_authoritative_resolution(self, relationship: CallRelationship) -> bool:
        """A Dart, Go or Lean caller's callee that the language's scoped resolution
        rejected is external or invisible; drop it so ast_parser's name-only
        fallback cannot bind it to an unrelated component."""
        caller = self.functions.get(relationship.caller)
        return caller is not None and caller.language in ("dart", "go", "lean")

    def _dotted_project_packages(self) -> dict[str, set]:
        """Project packages/namespaces, partitioned by language. Java and C#
        share the namespace-origin rule: a dotted callee qualified to a package
        with no prefix relation to any project package came from a third-party
        import."""
        packages: dict[str, set] = defaultdict(set)
        for func_info in self.functions.values():
            if func_info.language in ("java", "csharp"):
                package = self._dotted_package_for_node(func_info)
                if package:
                    packages[func_info.language].add(package)
        return packages

    def _is_external_callee(
        self, language: str | None, callee: str, dotted_packages: dict[str, set]
    ) -> bool:
        """Classify a still-unresolved callee as external, after project
        resolution has had its chance.

        Rules are generic, not name lists: prefix/standard-library knowledge in
        is_external_symbol, the C/C++ ALL_CAPS macro convention (macros are
        never components, so such calls can never resolve), and Java package
        origin — a dotted name qualified to a package with no prefix relation
        to any project package came from a third-party import.
        """
        if is_external_symbol(language, callee):
            return True
        if language in ("c", "cpp") and is_macro_name(callee):
            return True
        if language in ("java", "csharp") and "." in callee and dotted_packages.get(language):
            package = callee.rsplit(".", 1)[0]
            if not any(
                package == project
                or package.startswith(project + ".")
                or project.startswith(package + ".")
                for project in dotted_packages[language]
            ):
                return True
        if language == "python" and "." in callee:
            return self._is_external_python_callee(callee)
        if language == "dart":
            from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
                is_private_dart_name,
            )

            # A library-private name that did not resolve inside its library
            # can never be a component elsewhere; dropping it keeps the
            # name-based fallback from binding another library's `_Body`.
            if is_private_dart_name(callee):
                return True
        return False

    def _is_external_python_callee(self, callee: str) -> bool:
        """Classify a still-unresolved dotted Python callee.

        A callee whose dotted prefix aligns with a project module is an
        honest project gap, never external. Otherwise the import-origin rule
        (head was imported from outside the project) and the core-object
        method rule (a tail like ``append``/``endswith`` on an unknowable
        receiver can never be a project component) apply.
        """
        from codewiki.src.be.dependency_analyzer.analyzers.python import is_project_import
        from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
            PYTHON_OBJECT_METHODS,
        )

        parts = callee.split(".")
        if self._python_project_modules:
            for end in range(len(parts) - 1, 0, -1):
                if is_project_import(".".join(parts[:end]), self._python_project_modules):
                    return False
        if parts[0] in self._python_external_import_roots:
            return True
        return parts[-1] in PYTHON_OBJECT_METHODS

    def _build_resolution_indexes(self) -> dict[str, dict]:
        """Build exact/simple-name lookup indexes, both globally and per
        language. Resolution prefers the caller's own language partition: a
        name that is unique within the caller's language resolves even when
        another language defines the same name, and names made ambiguous only
        by foreign-language components keep resolving as before."""

        def make() -> dict[str, dict[str, list[str]]]:
            return {"exact": defaultdict(list), "simple": defaultdict(list)}

        global_indexes = make()
        by_lang: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(make)

        def add(index: dict[str, list[str]], key: str | None, func_id: str) -> None:
            if key and func_id not in index[key]:
                index[key].append(func_id)

        for func_id, func_info in self.functions.items():
            targets = [global_indexes]
            if func_info.language:
                targets.append(by_lang[func_info.language])

            exact_keys = [
                func_id,
                func_info.component_id,
                func_info.qualified_name,
                func_info.name,
            ]

            names = {func_info.name}
            if func_info.component_id:
                names.add(func_info.component_id.split("::")[-1])
            if func_info.qualified_name:
                names.add(func_info.qualified_name.split(".")[-1])
                parts = func_info.qualified_name.split(".")
                if len(parts) >= 2:
                    names.add(".".join(parts[-2:]))

            for target in targets:
                for key in exact_keys:
                    add(target["exact"], key, func_id)
                for name in names:
                    add(target["simple"], name, func_id)
                    if name and "." in name:
                        add(target["simple"], name.split(".")[-1], func_id)

        return {
            "exact": global_indexes["exact"],
            "simple": global_indexes["simple"],
            "by_lang": dict(by_lang),
        }

    def _resolve_callee(
        self, relationship: CallRelationship, indexes: dict[str, dict]
    ) -> str | None:
        caller = self.functions.get(relationship.caller)
        caller_language = caller.language if caller else None

        lang_indexes = indexes["by_lang"].get(caller_language) if caller_language else None
        if caller_language == "dart" and caller is not None:
            # Dart resolution is authoritative: it never falls back to name
            # guessing in other languages or invisible libraries.
            return self._resolve_dart_callee(relationship.callee, caller, lang_indexes)
        if caller_language == "go" and caller is not None:
            # So is Go's: a name resolves only in the package it names.
            return self._resolve_go_callee(relationship.callee, caller)
        if caller_language == "lean" and caller is not None:
            # And Lean's: only an exact FQN in a visible file.
            return self._resolve_lean_callee(relationship.callee, caller)
        if lang_indexes:
            match = self._resolve_callee_in(
                relationship, lang_indexes["exact"], lang_indexes["simple"]
            )
            if match:
                return match

        return self._resolve_callee_in(relationship, indexes["exact"], indexes["simple"])

    def _resolve_callee_in(
        self,
        relationship: CallRelationship,
        exact: dict[str, list[str]],
        simple: dict[str, list[str]],
    ) -> str | None:
        callee_name = relationship.callee

        exact_match = self._unique_match(exact, callee_name)
        if exact_match:
            return exact_match

        if "::" in callee_name:
            suffix = callee_name.split("::")[-1]
            exact_match = self._unique_match(exact, suffix)
            if exact_match:
                return exact_match
            simple_match = self._unique_match(simple, suffix)
            if simple_match:
                return simple_match

        if "." in callee_name:
            simple_match = self._unique_match(simple, callee_name)
            if simple_match:
                return simple_match
            tail_match = self._unique_match(simple, callee_name.split(".")[-1])
            if tail_match:
                return tail_match

        caller = self.functions.get(relationship.caller)
        if caller and caller.language in ("java", "csharp") and "." not in callee_name:
            package = self._dotted_package_for_node(caller)
            if package:
                same_package_match = self._unique_match(exact, f"{package}.{callee_name}")
                if same_package_match:
                    return same_package_match

        return self._unique_match(simple, callee_name)

    def _unique_match(self, index: dict[str, list[str]], key: str) -> str | None:
        matches = index.get(key, [])
        return matches[0] if len(matches) == 1 else None

    def _dotted_package_for_node(self, node: Node) -> str:
        """The package (Java) / namespace (C#) a node lives in, derived from its
        dotted qualified name. Both languages share the same shape: a type's
        package is its qualified name minus the type, a method's is minus
        ``Type.method``."""
        qualified_name = node.qualified_name or ""
        parts = qualified_name.split(".")
        if len(parts) < 2:
            return ""
        if node.component_type == "method" and len(parts) >= 3:
            return ".".join(parts[:-2])
        return ".".join(parts[:-1])

    def _caller_language(self, caller_id: str) -> str | None:
        caller = self.functions.get(caller_id)
        if caller and caller.language:
            return caller.language
        if caller:
            return CODE_EXTENSIONS.get(Path(caller.file_path).suffix.lower())
        return None

    def _deduplicate_relationships(self):
        """
        Deduplicate call relationships based on caller-callee pairs.

        Removes duplicate relationships while preserving the first occurrence.
        This helps eliminate noise from multiple calls to the same function.
        """
        seen = set()
        unique_relationships = []

        for rel in self.call_relationships:
            key = (rel.caller, rel.callee)
            if key not in seen:
                seen.add(key)
                unique_relationships.append(rel)

        self.call_relationships = unique_relationships

    def _generate_visualization_data(self) -> dict:
        """
        Generate visualization data for graph rendering.

        Creates Cytoscape.js compatible graph data with nodes and edges.

        Returns:
            Dict: Visualization data with cytoscape elements and summary
        """
        cytoscape_elements = []

        for func_id, func_info in self.functions.items():
            node_classes = []
            if func_info.node_type == "method":
                node_classes.append("node-method")
            else:
                node_classes.append("node-function")

            file_ext = Path(func_info.file_path).suffix.lower()
            language = func_info.language or CODE_EXTENSIONS.get(file_ext, "unknown")
            if file_ext == ".py":
                node_classes.append("lang-python")
            elif file_ext == ".js":
                node_classes.append("lang-javascript")
            elif file_ext == ".ts":
                node_classes.append("lang-typescript")
            elif language == "c":
                node_classes.append("lang-c")
            elif language == "cpp" or file_ext in [
                ".cpp",
                ".cc",
                ".cxx",
                ".c++",
                ".hpp",
                ".hxx",
                ".h++",
            ]:
                node_classes.append("lang-cpp")
            elif file_ext in [".kt", ".kts"]:
                node_classes.append("lang-kotlin")
            elif file_ext in [".php", ".phtml", ".inc"]:
                node_classes.append("lang-php")
            elif file_ext == ".rb":
                node_classes.append("lang-ruby")
            elif file_ext in [".scala", ".sc"]:
                node_classes.append("lang-scala")
            elif file_ext == ".rs":
                node_classes.append("lang-rust")
            elif file_ext == ".dart":
                node_classes.append("lang-dart")
            elif file_ext == ".go":
                node_classes.append("lang-go")
            elif file_ext == ".zig":
                node_classes.append("lang-zig")
            elif file_ext == ".lean":
                node_classes.append("lang-lean")
            elif file_ext == ".vue":
                node_classes.append("lang-vue")

            cytoscape_elements.append(
                {
                    "data": {
                        "id": func_id,
                        "label": func_info.name,
                        "file": func_info.file_path,
                        "type": func_info.node_type or "function",
                        "language": language,
                    },
                    "classes": " ".join(node_classes),
                }
            )

        resolved_rels = [r for r in self.call_relationships if r.is_resolved]
        for rel in resolved_rels:
            cytoscape_elements.append(
                {
                    "data": {
                        "id": f"{rel.caller}->{rel.callee}",
                        "source": rel.caller,
                        "target": rel.callee,
                        "line": rel.call_line,
                    },
                    "classes": "edge-call",
                }
            )

        summary = {
            "total_nodes": len(self.functions),
            "total_edges": len(resolved_rels),
            "unresolved_calls": len(self.call_relationships) - len(resolved_rels),
        }

        return {
            "cytoscape": {"elements": cytoscape_elements},
            "summary": summary,
        }

    def generate_llm_format(self) -> dict:
        """Generate clean format optimized for LLM consumption."""
        return {
            "functions": [
                {
                    "name": func.name,
                    "file": Path(func.file_path).name,
                    "purpose": (func.docstring.split("\n")[0] if func.docstring else None),
                    "parameters": func.parameters,
                    "is_recursive": func.name
                    in [
                        rel.callee
                        for rel in self.call_relationships
                        if rel.caller.endswith(func.name)
                    ],
                }
                for func in self.functions.values()
            ],
            "relationships": {
                func.name: {
                    "calls": [
                        rel.callee.split(":")[-1]
                        for rel in self.call_relationships
                        if rel.caller.endswith(func.name) and rel.is_resolved
                    ],
                    "called_by": [
                        rel.caller.split(":")[-1]
                        for rel in self.call_relationships
                        if rel.callee.endswith(func.name) and rel.is_resolved
                    ],
                }
                for func in self.functions.values()
            },
        }

    def _select_most_connected_nodes(self, target_count: int):
        """
        Select the most connected nodes from the call graph.

        Args:
            target_count: The number of nodes to select
        """
        if len(self.functions) <= target_count:
            return

        if not self.call_relationships:
            logger.warning("No call relationships found - keeping all functions by name")
            func_ids = list(self.functions.keys())[:target_count]
            self.functions = {fid: func for fid, func in self.functions.items() if fid in func_ids}
            return

        graph = {}
        for rel in self.call_relationships:
            if rel.caller in self.functions and rel.caller not in graph:
                graph[rel.caller] = set()
            if rel.callee in self.functions and rel.callee not in graph:
                graph[rel.callee] = set()

            if rel.caller in graph and rel.callee in graph:
                graph[rel.caller].add(rel.callee)
                graph[rel.callee].add(rel.caller)

        degree_centrality = {}
        for func_id in self.functions:
            degree_centrality[func_id] = len(graph.get(func_id, set()))

        sorted_func_ids = sorted(degree_centrality, key=degree_centrality.get, reverse=True)

        selected_func_ids = sorted_func_ids[:target_count]

        len(self.functions)
        self.functions = {
            fid: func for fid, func in self.functions.items() if fid in selected_func_ids
        }

        len(self.call_relationships)
        self.call_relationships = [
            rel
            for rel in self.call_relationships
            if rel.caller in selected_func_ids and rel.callee in selected_func_ids
        ]
