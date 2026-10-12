"""Question answering over generated docs + source code (MCP tools, ``codewiki serve``)."""

from __future__ import annotations

import os
import threading
import time

from codewiki.src.be.qa.index import QAIndex

# How often a long-lived process (MCP server, ``codewiki serve``) re-checks
# the working tree for code changes made since the index was built.
FRESHNESS_RECHECK_SECONDS = 30.0

_cache: dict[str, tuple[QAIndex, float]] = {}
_lock = threading.Lock()


class DocsNotFoundError(ValueError):
    pass


def resolve_docs_dir(docs_dir: str | None = None) -> str:
    """The docs dir from the argument, then ``CODEWIKI_DOCS_DIR``, then ``./docs``."""
    path = docs_dir or os.environ.get("CODEWIKI_DOCS_DIR") or os.path.join(os.getcwd(), "docs")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(os.path.join(path, "module_tree.json")) and not os.path.isfile(
        os.path.join(path, "overview.md")
    ):
        raise DocsNotFoundError(
            f"No CodeWiki docs found in {path}. Generate them with `codewiki generate`, "
            "or pass docs_dir / set CODEWIKI_DOCS_DIR to the docs folder."
        )
    return path


def get_index(docs_dir: str | None = None, repo_path: str | None = None) -> QAIndex:
    """A cached index, rebuilt when the docs are rewritten or the code changes."""
    path = resolve_docs_dir(docs_dir)
    with _lock:
        cached = _cache.get(path)
        now = time.monotonic()
        if cached is not None:
            index, checked = cached
            if index.stamp != QAIndex.compute_stamp(path):
                cached = None
            elif now - checked > FRESHNESS_RECHECK_SECONDS:
                from codewiki.src.be.qa.freshness import detect

                current = detect(path, index.repo_path, {u.file for u in index.code.values()})
                if current.touched != index.freshness.touched or (
                    current.head_commit != index.freshness.head_commit
                ):
                    cached = None
                else:
                    _cache[path] = (index, now)
        if cached is None:
            index = QAIndex(path, repo_path)
            _cache[path] = (index, now)
        return _cache[path][0]


_backends: dict[tuple[str, str | None], object] = {}


def load_backend(index: QAIndex, model: str | None = None):
    """The LLM backend from the user's CodeWiki config (``codewiki config set``).

    Same provider resolution as ``codewiki generate``: claude-code / codex use
    the CLI subscription, other providers the stored API key.
    """
    key = (index.docs_dir, model)
    with _lock:
        if key in _backends:
            return _backends[key]
    from codewiki.cli.config_manager import ConfigManager
    from codewiki.src.be.backend import get_backend, is_caw_provider

    manager = ConfigManager()
    if not manager.load():
        raise RuntimeError("CodeWiki is not configured. Run `codewiki config set` first.")
    config = manager.get_config()
    api_key = manager.get_api_key()
    if not api_key and not is_caw_provider(getattr(config, "provider", "")):
        raise RuntimeError("No API key configured. Run `codewiki config set --api-key <key>`.")
    backend_config = config.to_backend_config(
        repo_path=index.repo_path or os.path.dirname(index.docs_dir),
        output_dir=index.docs_dir,
        api_key=api_key or "",
    )
    if model:
        backend_config.main_model = model
    backend = get_backend(backend_config)
    with _lock:
        _backends[key] = backend
    return backend


def get_engine(docs_dir: str | None = None, model: str | None = None, **engine_options):
    """A Q&A engine over the (cached) index of ``docs_dir``."""
    from codewiki.src.be.qa.engine import QAEngine

    index = get_index(docs_dir)
    return QAEngine(index, load_backend(index, model), **engine_options)
