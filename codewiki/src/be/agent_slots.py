"""Shared state for running module agents at the same time.

``AgentSlots`` caps how many agent sessions talk to the model at once
(``config.concurrency``). A parent agent that delegates to sub-agents is idle
until they finish, so it lends its slot to them (``lend``) instead of holding
it — otherwise a full pool of waiting parents would deadlock.

``SharedModuleTree`` is the one in-memory module tree every agent of a run
reads and extends. Agents used to load module_tree.json at start and save it
at the end, which is fine one at a time but lets concurrent agents overwrite
each other's sub-modules. All access goes through its re-entrant ``lock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

from codewiki.src.utils import file_manager

_POLL_SECONDS = 0.05


class AgentSlots:
    """A counting semaphore usable from threads and from the event loop."""

    def __init__(self, size: int) -> None:
        self.size = max(1, int(size or 1))
        self._sem = threading.BoundedSemaphore(self.size)

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()

    @contextlib.asynccontextmanager
    async def hold_async(self) -> AsyncIterator[None]:
        # Poll instead of asyncio.to_thread(acquire): hundreds of waiting
        # modules would otherwise park the default executor's threads.
        while not self._sem.acquire(blocking=False):
            await asyncio.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            self._sem.release()

    @contextlib.contextmanager
    def lend(self) -> Iterator[None]:
        """Give the caller's slot back while it waits on sub-agents."""
        self._sem.release()
        try:
            yield
        finally:
            self._sem.acquire()

    @contextlib.asynccontextmanager
    async def lend_async(self) -> AsyncIterator[None]:
        self._sem.release()
        try:
            yield
        finally:
            while not self._sem.acquire(blocking=False):
                await asyncio.sleep(_POLL_SECONDS)


def merge_module_tree(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Add to ``dst`` every module of ``src`` it lacks (agents only add modules)."""
    for name, info in src.items():
        if name not in dst:
            dst[name] = info
            continue
        src_children = info.get("children") if isinstance(info, dict) else None
        if not src_children:
            continue
        dst_info = dst[name]
        if not isinstance(dst_info.get("children"), dict):
            dst_info["children"] = {}
        merge_module_tree(dst_info["children"], src_children)


class SharedModuleTree:
    """module_tree.json held in memory for one run, saved under a lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._path: str | None = None
        self._tree: dict[str, Any] | None = None

    def get(self, path: str) -> dict[str, Any]:
        """The live tree for ``path``, re-read when the file changed under us."""
        with self.lock:
            if self._path != path or self._tree is None:
                self._tree = file_manager.load_json(path) if os.path.exists(path) else {}
                self._path = path
            else:
                # Someone else (the generator, the updater) rewrote the file:
                # keep their modules and ours.
                on_disk = file_manager.load_json(path) if os.path.exists(path) else {}
                merge_module_tree(self._tree, on_disk)
            return self._tree

    def save(self, path: str, tree: dict[str, Any]) -> None:
        with self.lock:
            if tree is not self._tree:
                live = self.get(path)
                merge_module_tree(live, tree)
                tree = live
            file_manager.save_json(tree, path)

    def snapshot(self, tree: dict[str, Any]) -> dict[str, Any]:
        """A private deep copy, safe to read while other agents extend the tree."""
        with self.lock:
            return json.loads(json.dumps(tree))
