"""
Repository Analyzer Module

This module provides functionality to analyze repository structures and generate
detailed file tree representations with filtering capabilities.
"""

import fnmatch
import logging
import os
import shutil
import subprocess
from pathlib import Path

from pathspec import GitIgnoreSpec

from codewiki.src.be.dependency_analyzer.utils.patterns import (
    ARTIFACT_WHITELIST,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_INCLUDE_PATTERNS,
)

logger = logging.getLogger(__name__)


class GitIgnoreFilter:
    """Evaluate Git ignore rules once per repository analysis.

    Git repositories use ``git ls-files`` so nested ignore files, repository
    excludes, global excludes, and tracked-file semantics exactly match Git.
    Plain directories fall back to pathspec-backed ``.gitignore`` evaluation.
    """

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir.resolve()
        self._ignored_files: set[str] = set()
        self._ignored_dirs: set[str] = set()
        self._ignore_all_untracked = False
        self._fallback_specs: list[tuple[str, GitIgnoreSpec]] = []
        self._using_git = self._load_git_ignored_paths()
        if not self._using_git:
            self._load_fallback_specs()

    def _load_git_ignored_paths(self) -> bool:
        git_path = shutil.which("git")
        if not git_path:
            logger.debug("Git is unavailable; falling back to direct .gitignore parsing")
            return False

        try:
            root_result = subprocess.run(
                [git_path, "-C", str(self.repo_dir), "rev-parse", "--show-toplevel"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if root_result.returncode != 0:
                logger.debug(
                    "%s is not in a Git worktree; using direct .gitignore parsing",
                    self.repo_dir,
                )
                return False
            worktree_root = Path(root_result.stdout.strip()).resolve()
            scope = self.repo_dir.relative_to(worktree_root).as_posix()
            scope = scope if scope != "." else "."

            ignored_result = subprocess.run(
                [
                    git_path,
                    "-C",
                    str(worktree_root),
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "--directory",
                    "-z",
                    "--",
                    scope,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning(
                "Could not query Git ignore rules for %s; falling back to direct parsing: %s",
                self.repo_dir,
                exc,
            )
            return False

        scope_prefix = "" if scope == "." else f"{scope.rstrip('/')}/"
        for raw_path in ignored_result.stdout.split(b"\0"):
            if not raw_path:
                continue
            repo_relative = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            if scope_prefix:
                if not repo_relative.startswith(scope_prefix):
                    continue
                relative = repo_relative[len(scope_prefix) :]
            else:
                relative = repo_relative

            if not relative and repo_relative.endswith("/"):
                self._ignore_all_untracked = True
            elif relative.endswith("/"):
                self._ignored_dirs.add(relative.rstrip("/"))
            else:
                self._ignored_files.add(relative)

        return True

    def _load_fallback_specs(self) -> None:
        try:
            candidates = sorted(
                (
                    path
                    for path in self.repo_dir.rglob(".gitignore")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: (len(path.relative_to(self.repo_dir).parts), path.as_posix()),
            )
        except OSError as exc:
            logger.warning("Could not discover .gitignore files under %s: %s", self.repo_dir, exc)
            return

        for gitignore_path in candidates:
            try:
                lines = gitignore_path.read_text(encoding="utf-8").splitlines()
                spec = GitIgnoreSpec.from_lines(lines)
                base = gitignore_path.parent.relative_to(self.repo_dir).as_posix()
                self._fallback_specs.append(("" if base == "." else base, spec))
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning("Skipping unreadable .gitignore at %s: %s", gitignore_path, exc)

    def is_ignored(self, relative_path: str, is_dir: bool) -> bool:
        """Return whether a repository-relative path should be ignored."""
        normalized = relative_path.replace("\\", "/")
        normalized = normalized.removeprefix("./")
        if normalized in ("", "."):
            return False

        if self._using_git:
            if self._ignore_all_untracked:
                return True
            if normalized in self._ignored_files or normalized in self._ignored_dirs:
                return True
            return any(
                normalized.startswith(f"{ignored_dir}/") for ignored_dir in self._ignored_dirs
            )

        ignored = False
        for base, spec in self._fallback_specs:
            if base:
                if normalized == base:
                    local_path = ""
                elif normalized.startswith(f"{base}/"):
                    local_path = normalized[len(base) + 1 :]
                else:
                    continue
            else:
                local_path = normalized

            if not local_path:
                continue
            candidate = f"{local_path}/" if is_dir else local_path
            result = spec.check_file(candidate)
            if result.include is not None:
                ignored = bool(result.include)

        return ignored


class RepoAnalyzer:
    def __init__(
        self,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        use_gitignore: bool = True,
    ) -> None:
        # Include patterns: if specified, use ONLY those patterns (replaces defaults)
        self.include_patterns = (
            include_patterns if include_patterns is not None else DEFAULT_INCLUDE_PATTERNS
        )
        # Exclude patterns: if specified, MERGE with default ignore patterns.
        # The two sets are also kept apart: user excludes always win, while
        # the defaults yield to ARTIFACT_WHITELIST (e.g. `.github/workflows`).
        self.default_exclude_patterns = list(DEFAULT_IGNORE_PATTERNS)
        self.user_exclude_patterns = list(exclude_patterns) if exclude_patterns is not None else []
        self.exclude_patterns = self.default_exclude_patterns + self.user_exclude_patterns
        self.use_gitignore = use_gitignore
        self._gitignore_filter: GitIgnoreFilter | None = None

    def analyze_repository_structure(self, repo_dir: str) -> dict:
        self._gitignore_filter = GitIgnoreFilter(Path(repo_dir)) if self.use_gitignore else None
        file_tree = self._build_file_tree(repo_dir)
        return {
            "file_tree": file_tree,
            "summary": {
                "total_files": self._count_files(file_tree),
                "total_size_kb": self._calculate_size(file_tree),
            },
        }

    def _build_file_tree(self, repo_dir: str) -> dict:
        def build_tree(path: Path, base_path: Path) -> dict | None:
            relative_path = path.relative_to(base_path)
            relative_path_str = str(relative_path)

            # 🚫 Reject symlinks
            if path.is_symlink():
                return None

            # 🚫 Reject escaped paths (e.g., symlinks pointing outside)
            try:
                if not path.resolve().is_relative_to(base_path.resolve()):
                    return None
            except AttributeError:
                if not str(path.resolve()).startswith(str(base_path.resolve())):
                    return None

            if self._should_exclude_path(relative_path_str, path.name, path.is_dir()):
                return None

            if path.is_file():
                if not self._should_include_file(relative_path_str, path.name):
                    return None

                size = path.stat().st_size
                return {
                    "type": "file",
                    "name": path.name,
                    "path": relative_path_str,
                    "extension": path.suffix,
                    "_size_bytes": size,
                }

            elif path.is_dir():
                children = []
                try:
                    for child in sorted(path.iterdir()):
                        child_tree = build_tree(child, base_path)
                        if child_tree is not None:
                            children.append(child_tree)
                except PermissionError:
                    pass

                if children or str(relative_path) == ".":
                    return {
                        "type": "directory",
                        "name": path.name,
                        "path": relative_path_str,
                        "children": children,
                    }
                return None

            # Other types (sockets, devices, etc.)
            return None

        return build_tree(Path(repo_dir), Path(repo_dir))

    @staticmethod
    def _matches_any(path: str, filename: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(filename, pattern):
                return True
            if pattern.endswith("/") and path.startswith(pattern.rstrip("/")):
                return True
            if path.startswith(pattern + "/") or path == pattern:
                return True
            if pattern in path.split("/"):
                return True
        return False

    @staticmethod
    def _is_artifact_whitelisted(path: str, filename: str, is_dir: bool) -> bool:
        """True when ``path`` is an artifact the default ignore list must not drop.

        Directories count as whitelisted when a whitelist pattern lives below
        them, so ``.github`` survives long enough for ``.github/workflows/*``
        to be visited.
        """
        norm = path.replace(os.sep, "/")
        for pattern in ARTIFACT_WHITELIST:
            if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(filename, pattern):
                return True
            if is_dir and "/" in pattern and pattern.startswith(norm.rstrip("/") + "/"):
                return True
        return False

    def _should_exclude_path(self, path: str, filename: str, is_dir: bool = False) -> bool:
        # User-provided excludes always win.
        if self._matches_any(path, filename, self.user_exclude_patterns):
            return True
        # Built-in ignores yield to the artifact whitelist.
        if not self._is_artifact_whitelisted(path, filename, is_dir) and self._matches_any(
            path, filename, self.default_exclude_patterns
        ):
            return True
        return bool(self._gitignore_filter and self._gitignore_filter.is_ignored(path, is_dir))

    def _should_include_file(self, path: str, filename: str) -> bool:
        if not self.include_patterns:
            return True
        for pattern in self.include_patterns:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(filename, pattern):
                return True
        return False

    def _count_files(self, tree: dict) -> int:
        if tree["type"] == "file":
            return 1
        return sum(self._count_files(child) for child in tree.get("children", []))

    def _calculate_size(self, tree: dict) -> float:
        if tree["type"] == "file":
            return tree.get("_size_bytes", 0) / 1024
        return sum(self._calculate_size(child) for child in tree.get("children", []))
