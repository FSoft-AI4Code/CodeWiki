"""Load and snapshot the saved dependency graph.

``DependencyGraphBuilder.build_dependency_graph`` writes the graph to
``<output>/temp/dependency_graphs/<repo>_dependency_graph.json`` and
overwrites it on every run. The updater therefore copies the previous
graph aside *before* the new graph is built, then loads that copy.
"""

from __future__ import annotations

import json
import logging
import os
import shutil

from codewiki.src.be.dependency_analyzer.models.core import Node

logger = logging.getLogger(__name__)

PREV_SUFFIX = ".prev.json"


def sanitize_repo_name(repo_path: str) -> str:
    repo_name = os.path.basename(os.path.normpath(repo_path))
    return "".join(c if c.isalnum() else "_" for c in repo_name)


def graph_file_path(dependency_graph_dir: str, repo_path: str) -> str:
    """Path of the graph JSON for ``repo_path`` inside ``dependency_graph_dir``."""
    return os.path.join(
        dependency_graph_dir, f"{sanitize_repo_name(repo_path)}_dependency_graph.json"
    )


def prev_graph_path(dependency_graph_dir: str, repo_path: str) -> str:
    return graph_file_path(dependency_graph_dir, repo_path)[: -len(".json")] + PREV_SUFFIX


def list_graph_files(dependency_graph_dir: str) -> list[str]:
    """All current ``*_dependency_graph.json`` files in the dir (``.prev.json`` excluded)."""
    if not os.path.isdir(dependency_graph_dir):
        return []
    return sorted(
        os.path.join(dependency_graph_dir, f)
        for f in os.listdir(dependency_graph_dir)
        if f.endswith("_dependency_graph.json")
    )


def find_any_graph_file(dependency_graph_dir: str) -> str | None:
    """Return the previous build's graph when it is not under the current repo name.

    The repo may have been analysed from a differently named checkout (a git
    worktree per revision, for example), so the sanitized name is not always
    the same. With exactly one candidate that is the answer. With several
    (each earlier worktree left its own file) the newest by mtime is taken and
    the choice is logged; ``prune_superseded_graphs`` keeps this case rare.
    """
    candidates = list_graph_files(dependency_graph_dir)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    newest = max(candidates, key=os.path.getmtime)
    logger.warning(
        "Several dependency graphs found in %s; using the newest (%s) as the previous graph. "
        "Candidates: %s",
        dependency_graph_dir,
        os.path.basename(newest),
        ", ".join(os.path.basename(c) for c in candidates),
    )
    return newest


def prune_superseded_graphs(dependency_graph_dir: str, keep: str) -> list[str]:
    """Delete current-graph files other than ``keep`` so the next update finds one graph.

    The previous graph survives as ``*.prev.json``; only stale copies left by
    earlier checkouts under other names are removed. Returns the removed paths.
    """
    removed = []
    keep_abs = os.path.abspath(keep)
    for path in list_graph_files(dependency_graph_dir):
        if os.path.abspath(path) != keep_abs:
            os.remove(path)
            removed.append(path)
    if removed:
        logger.info(
            "Removed superseded dependency graph(s): %s",
            ", ".join(os.path.basename(r) for r in removed),
        )
    return removed


def snapshot_old_graph(dependency_graph_dir: str, repo_path: str) -> str | None:
    """Copy the previous build's graph to ``*.prev.json``; return that path or None."""
    src = graph_file_path(dependency_graph_dir, repo_path)
    if not os.path.exists(src):
        src = find_any_graph_file(dependency_graph_dir)
    if src is None or not os.path.exists(src):
        return None
    dst = prev_graph_path(dependency_graph_dir, repo_path)
    shutil.copyfile(src, dst)
    logger.info("Saved previous dependency graph to %s", dst)
    return dst


def load_graph(path: str) -> dict[str, Node]:
    """Read a saved graph JSON back into ``Node`` objects keyed by component id."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    graph: dict[str, Node] = {}
    for cid, data in raw.items():
        if not isinstance(data, dict):
            continue
        data = dict(data)
        deps = data.get("depends_on") or []
        data["depends_on"] = set(deps)
        data.setdefault("id", cid)
        graph[cid] = Node(**data)
    return graph


def save_graph(graph: dict[str, Node], path: str) -> None:
    """Write ``graph`` in the same shape ``DependencyParser.save_dependency_graph`` uses."""
    result = {}
    for cid, node in graph.items():
        d = node.model_dump()
        if isinstance(d.get("depends_on"), set):
            d["depends_on"] = sorted(d["depends_on"])
        result[cid] = d
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
