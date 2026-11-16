"""Disk cache for per-batch LLM clustering partial module trees."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from copy import deepcopy
from typing import Any

from codewiki.src.config import CLUSTERING_BATCH_CACHE_FILENAME, Config
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)

CACHE_VERSION = 4


def clustering_cache_scope(module_path: list[str]) -> str:
    """Stable scope id for one ``cluster_modules`` invocation."""
    if not module_path:
        return "__root__"
    return "/".join(module_path)


def clustering_request_fingerprint(config: Config, prompt: str) -> str:
    """Identify the rendered request and configured routing without persisting secrets."""
    routing = {
        name: getattr(config, name, None)
        for name in (
            "provider",
            "cluster_model",
            "main_model",
            "llm_base_url",
            "azure_deployment",
            "api_version",
            "aws_region",
            "google_project",
            "google_location",
            "max_tokens",
        )
    }
    if routing["provider"] == "google-vertex":
        routing["google_project"] = routing["google_project"] or os.getenv(
            "GOOGLE_CLOUD_PROJECT", ""
        )
        routing["google_location"] = (
            routing["google_location"] or os.getenv("GOOGLE_CLOUD_LOCATION") or "global"
        )
    context = {
        "repo": os.path.realpath(config.repo_path),
        "routing": routing,
        "prompt": prompt,
    }
    canonical = json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def batch_entry_key(
    scope: str, batch_index: int, node_ids: list[str], request_fingerprint: str
) -> str:
    """Scope, batch index, component set, and exact request identity."""
    canonical = json.dumps(sorted(node_ids), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{scope}|{batch_index}|{digest}|{request_fingerprint}"


def clustering_batch_cache_path(docs_dir: str) -> str:
    return os.path.join(os.path.abspath(docs_dir), "temp", CLUSTERING_BATCH_CACHE_FILENAME)


def delete_clustering_batch_cache(docs_dir: str, *, scopes: list[str] | None = None) -> None:
    """Remove committed scopes and descendants, or the whole cache after a full build."""
    if not docs_dir:
        return
    if scopes is not None:
        if scopes:
            ClusterBatchCache(docs_dir).delete_scopes(scopes)
        return
    _delete_cache_file(clustering_batch_cache_path(docs_dir))


def _delete_cache_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove clustering batch cache at %s: %s", path, exc)
        return
    logger.info("Removed clustering batch cache at %s", path)


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    ids = entry.get("node_ids")
    partial = entry.get("partial_tree")
    return (
        isinstance(entry.get("scope"), str)
        and isinstance(entry.get("request_fingerprint"), str)
        and bool(entry["request_fingerprint"])
        and isinstance(entry.get("batch_index"), int)
        and not isinstance(entry["batch_index"], bool)
        and entry["batch_index"] > 0
        and isinstance(ids, list)
        and all(isinstance(cid, str) for cid in ids)
        and isinstance(partial, dict)
        and bool(partial)
        and all(
            isinstance(name, str)
            and isinstance(info, dict)
            and isinstance(info.get("path", ""), str)
            and isinstance(info.get("components"), list)
            and all(isinstance(cid, str) and cid in ids for cid in info["components"])
            for name, info in partial.items()
        )
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
        try:
            raw = file_manager.load_json(self._path)
        except (OSError, ValueError, UnicodeError) as exc:
            logger.warning("Ignoring unreadable clustering batch cache at %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            entries = {key: entry for key, entry in entries.items() if _valid_entry(entry)}
            self._data = {"version": CACHE_VERSION, "entries": entries}
            logger.info(
                "Loaded clustering batch cache: %d entries from %s",
                len(entries),
                self._path,
            )

    def _save(self) -> bool:
        """Publish a complete checkpoint while keeping the previous one on interruption."""
        temporary_path = None
        try:
            directory = os.path.dirname(self._path)
            file_manager.ensure_directory(directory)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, prefix=".clustering-cache-", delete=False
            ) as stream:
                temporary_path = stream.name
                json.dump(self._data, stream, indent=4, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
            return True
        except OSError as exc:
            logger.warning("Could not save clustering batch cache at %s: %s", self._path, exc)
            return False
        finally:
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning("Could not remove temporary clustering cache: %s", exc)

    def entry_count(self) -> int:
        entries = self._data.get("entries")
        return len(entries) if isinstance(entries, dict) else 0

    def scope_entry_count(self, scope: str) -> int:
        entries = self._data.get("entries")
        if not isinstance(entries, dict):
            return 0
        return sum(1 for entry in entries.values() if entry.get("scope") == scope)

    def delete_scopes(self, scopes: list[str]) -> None:
        """Keep checkpoints for failed or unrelated subtrees available for retry."""
        if not self._enabled or not scopes:
            return
        selected = set(scopes)
        prefixes = tuple(scope + "/" for scope in selected)
        entries = self._data["entries"]
        remaining = {
            key: entry
            for key, entry in entries.items()
            if entry["scope"] not in selected and not entry["scope"].startswith(prefixes)
        }
        if len(remaining) == len(entries):
            return
        self._data["entries"] = remaining
        if remaining:
            self._save()
        else:
            _delete_cache_file(self._path)

    def lookup(
        self, scope: str, batch_index: int, node_ids: list[str], *, request_fingerprint: str
    ) -> dict[str, Any] | None:
        if not self._enabled:
            return None
        key = batch_entry_key(scope, batch_index, node_ids, request_fingerprint)
        entry = self._data["entries"].get(key)
        if not _valid_entry(entry):
            return None
        stored_ids = entry.get("node_ids")
        if (
            stored_ids != sorted(node_ids)
            or entry["scope"] != scope
            or entry["batch_index"] != batch_index
            or entry["request_fingerprint"] != request_fingerprint
        ):
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
        return deepcopy(partial)

    def store(
        self,
        scope: str,
        batch_index: int,
        node_ids: list[str],
        partial_tree: dict[str, Any],
        *,
        request_fingerprint: str,
    ) -> None:
        if not self._enabled or not partial_tree:
            return
        key = batch_entry_key(scope, batch_index, node_ids, request_fingerprint)
        entry = {
            "scope": scope,
            "batch_index": batch_index,
            "node_ids": sorted(node_ids),
            "request_fingerprint": request_fingerprint,
            "partial_tree": deepcopy(partial_tree),
        }
        if not _valid_entry(entry):
            return
        self._data["entries"][key] = entry
        if not self._save():
            return
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
