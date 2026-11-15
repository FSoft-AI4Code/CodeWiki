"""Disk cache for per-batch LLM clustering partial module trees."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from codewiki.src.config import CLUSTERING_BATCH_CACHE_FILENAME
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


def clustering_cache_scope(module_path: list[str]) -> str:
    """Stable scope id for one ``cluster_modules`` invocation."""
    if not module_path:
        return "__root__"
    return "/".join(module_path)


def batch_entry_key(scope: str, batch_index: int, node_ids: list[str]) -> str:
    """Scope, 1-based batch index, and a fingerprint of the batch's component IDs."""
    canonical = "\n".join(sorted(node_ids))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{scope}|{batch_index}|{digest}"


def clustering_batch_cache_path(docs_dir: str) -> str:
    return os.path.join(os.path.abspath(docs_dir), "temp", CLUSTERING_BATCH_CACHE_FILENAME)


def delete_clustering_batch_cache(docs_dir: str) -> None:
    """Remove the on-disk batch cache after clustering finished successfully."""
    if not docs_dir:
        return
    path = clustering_batch_cache_path(docs_dir)
    if not os.path.isfile(path):
        return
    raw = file_manager.load_json(path)
    entry_count = len((raw or {}).get("entries") or {})
    os.remove(path)
    logger.info(
        "Removed clustering batch cache (%d entries) at %s",
        entry_count,
        path,
    )


class ClusterBatchCache:
    """Read/write partial clustering results keyed by scope, batch index, and node set."""

    def __init__(self, docs_dir: str, *, enabled: bool = True) -> None:
        self._enabled = enabled and bool(docs_dir)
        self._path = clustering_batch_cache_path(docs_dir) if self._enabled else ""
        self._data: dict[str, Any] = {"version": CACHE_VERSION, "entries": {}}
        if self._enabled:
            self._load()

    def _load(self) -> None:
        raw = file_manager.load_json(self._path)
        if not raw or raw.get("version") != CACHE_VERSION:
            if os.path.isfile(self._path):
                logger.info(
                    "Ignoring clustering batch cache at %s (missing or unsupported version).",
                    self._path,
                )
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._data = {"version": CACHE_VERSION, "entries": entries}
            logger.info(
                "Loaded clustering batch cache: %d entries from %s",
                len(entries),
                self._path,
            )

    def entry_count(self) -> int:
        entries = self._data.get("entries")
        return len(entries) if isinstance(entries, dict) else 0

    def scope_entry_count(self, scope: str) -> int:
        entries = self._data.get("entries")
        if not isinstance(entries, dict):
            return 0
        return sum(1 for entry in entries.values() if entry.get("scope") == scope)

    def lookup(self, scope: str, batch_index: int, node_ids: list[str]) -> dict[str, Any] | None:
        if not self._enabled:
            return None
        key = batch_entry_key(scope, batch_index, node_ids)
        entry = self._data["entries"].get(key)
        if not entry:
            return None
        stored_ids = entry.get("node_ids")
        if stored_ids != sorted(node_ids):
            return None
        partial = entry.get("partial_tree")
        if not isinstance(partial, dict) or not partial:
            return None
        logger.info(
            "Using cached clustering result for scope %s batch %d (%d components).",
            scope,
            batch_index,
            len(node_ids),
        )
        return partial

    def store(
        self,
        scope: str,
        batch_index: int,
        node_ids: list[str],
        partial_tree: dict[str, Any],
    ) -> None:
        if not self._enabled or not partial_tree:
            return
        key = batch_entry_key(scope, batch_index, node_ids)
        self._data["entries"][key] = {
            "scope": scope,
            "batch_index": batch_index,
            "node_ids": sorted(node_ids),
            "partial_tree": partial_tree,
        }
        file_manager.ensure_directory(os.path.dirname(self._path))
        file_manager.save_json(self._data, self._path)
        module_names = list(partial_tree.keys())
        logger.info(
            "Stored clustering batch cache for scope %s batch %d (%d components, %d modules: %s); "
            "%d total entries at %s",
            scope,
            batch_index,
            len(node_ids),
            len(module_names),
            ", ".join(module_names[:5]) + ("..." if len(module_names) > 5 else ""),
            self.entry_count(),
            self._path,
        )
