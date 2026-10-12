"""Did the code change since the docs were generated?

The docs and the dependency graph are a snapshot of the commit recorded in
``metadata.json`` (``generation_info.commit_id``). Questions must still be
answered from the code as it is now, so this finds the files that changed
since then: with git, the diff from that commit to the working tree plus
untracked files; without git, files modified after the generation time.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

from codewiki.src.be.doc_layout import METADATA_FILENAME

_GIT_TIMEOUT = 15


@dataclass
class Freshness:
    method: str  # "git" | "mtime" | "none"
    docs_commit: str | None = None
    head_commit: str | None = None
    commits_behind: int | None = None
    changed: set[str] = field(default_factory=set)  # modified or added, repo-relative
    deleted: set[str] = field(default_factory=set)
    checked_at: float = field(default_factory=time.time)

    @property
    def touched(self) -> set[str]:
        return self.changed | self.deleted

    def summary(self) -> str:
        if self.method == "none":
            return "freshness unknown (no repository checkout or generation commit)"
        parts = []
        if self.docs_commit:
            parts.append(f"docs at {self.docs_commit[:7]}")
        if self.commits_behind:
            parts.append(f"{self.commits_behind} commit(s) behind")
        files = len(self.touched)
        parts.append(f"{files} file(s) changed since" if files else "up to date with the code")
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "docs_commit": self.docs_commit,
            "head_commit": self.head_commit,
            "commits_behind": self.commits_behind,
            "changed": sorted(self.changed),
            "deleted": sorted(self.deleted),
            "summary": self.summary(),
        }


def read_generation_info(docs_dir: str) -> dict:
    try:
        with open(os.path.join(docs_dir, METADATA_FILENAME), encoding="utf-8") as f:
            return (json.load(f) or {}).get("generation_info") or {}
    except (OSError, ValueError):
        return {}


def _git(repo: str, *args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 — fixed git argv, no shell
            ["git", "-C", repo, *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _inside(path: str, folder: str | None) -> bool:
    if not folder:
        return False
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def detect(docs_dir: str, repo_path: str | None, known_files: set[str] | None = None) -> Freshness:
    """Files changed since the docs' generation commit.

    ``known_files`` (repo-relative paths in the dependency graph) is what the
    mtime fallback checks; git needs no list. Paths inside the docs directory
    itself are ignored: regenerating pages is not a code change.
    """
    info = read_generation_info(docs_dir)
    commit = info.get("commit_id") or None
    if not repo_path or not os.path.isdir(repo_path):
        return Freshness(method="none", docs_commit=commit)

    docs_rel = os.path.relpath(os.path.abspath(docs_dir), os.path.abspath(repo_path))
    docs_rel = None if docs_rel.startswith("..") else docs_rel.replace(os.sep, "/")

    if commit and _git(repo_path, "cat-file", "-e", f"{commit}^{{commit}}") is not None:
        changed: set[str] = set()
        deleted: set[str] = set()
        diff = _git(repo_path, "diff", "--name-status", "--relative", "--no-renames", commit)
        for line in (diff or "").splitlines():
            status, _, path = line.partition("\t")
            if not path or _inside(path, docs_rel):
                continue
            (deleted if status.startswith("D") else changed).add(path)
        untracked = _git(repo_path, "ls-files", "--others", "--exclude-standard")
        for path in (untracked or "").splitlines():
            if path and not _inside(path, docs_rel):
                changed.add(path)
        head = (_git(repo_path, "rev-parse", "HEAD") or "").strip() or None
        behind = _git(repo_path, "rev-list", "--count", f"{commit}..HEAD")
        return Freshness(
            method="git",
            docs_commit=commit,
            head_commit=head,
            commits_behind=int(behind.strip()) if behind and behind.strip().isdigit() else None,
            changed=changed,
            deleted=deleted,
        )

    stamp = info.get("timestamp")
    try:
        generated = datetime.fromisoformat(stamp).timestamp() if stamp else None
    except ValueError:
        generated = None
    if generated is None:
        return Freshness(method="none", docs_commit=commit)
    changed, deleted = set(), set()
    for rel in known_files or ():
        path = os.path.join(repo_path, rel)
        try:
            if os.path.getmtime(path) > generated:
                changed.add(rel)
        except OSError:
            deleted.add(rel)
    return Freshness(method="mtime", docs_commit=commit, changed=changed, deleted=deleted)
