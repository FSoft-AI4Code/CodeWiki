"""Reference index: which pages and component ids each page links to or names.

Built by parsing the markdown after each write. ``inverse`` answers "who
refers to x" for pages, component ids, and bare component names.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater import tree as T

INDEX_FILENAME = "reference_index.json"

_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>#]+\.md)(?:#[^)]*)?>?\s*\)")
_BARE_MD_RE = re.compile(r"(?<![\w/])([A-Za-z0-9_\-]+)\.md\b")
_ID_RE = re.compile(r"[\w./\-]+::[\w.$<>-]+")
_CODE_RE = re.compile(r"`([^`\n]{2,120})`")
_WORD_RE = re.compile(r"[A-Za-z_][\w]*")


def index_path(docs_dir: str) -> str:
    return os.path.join(docs_dir, "temp", INDEX_FILENAME)


def _page_stems(docs_dir: str) -> list[str]:
    try:
        return sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(docs_dir)
            if f.endswith(".md") and not f.startswith(".")
        )
    except OSError:
        return []


def extract_references(
    text: str,
    known_ids: set[str],
    known_names: dict[str, set[str]],
    known_pages: set[str],
) -> dict[str, list[str]]:
    """Return ``links`` (page stems), ``ids`` (component ids), ``names`` (bare names)."""
    links: set[str] = set()
    for m in _LINK_RE.finditer(text):
        stem = os.path.splitext(os.path.basename(m.group(1)))[0]
        links.add(stem)
    for m in _BARE_MD_RE.finditer(text):
        if m.group(1) in known_pages:
            links.add(m.group(1))
    ids = {m.group(0) for m in _ID_RE.finditer(text) if m.group(0) in known_ids}
    names: set[str] = set()
    for m in _CODE_RE.finditer(text):
        for w in _WORD_RE.findall(m.group(1)):
            if w in known_names:
                names.add(w)
    return {"links": sorted(links), "ids": sorted(ids), "names": sorted(names)}


def build_reference_index(
    docs_dir: str, graph: dict[str, Node], tree: dict[str, Any] | None = None
) -> dict[str, dict[str, list[str]]]:
    known_ids = set(graph)
    known_names: dict[str, set[str]] = {}
    for cid, node in graph.items():
        name = node.name
        if name and len(name) >= 3:
            known_names.setdefault(name, set()).add(cid)
    pages = _page_stems(docs_dir)
    known_pages = set(pages)
    if tree is not None:
        known_pages |= {p[-1] for p, _ in T.iter_nodes(tree)}
    index: dict[str, dict[str, list[str]]] = {}
    for stem in pages:
        try:
            with open(os.path.join(docs_dir, f"{stem}.md"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        index[stem] = extract_references(text, known_ids, known_names, known_pages)
    return index


def save_reference_index(index: dict[str, Any], docs_dir: str) -> str:
    path = index_path(docs_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False, sort_keys=True)
    return path


def load_reference_index(docs_dir: str) -> dict[str, dict[str, list[str]]] | None:
    path = index_path(docs_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def inverse(index: dict[str, dict[str, list[str]]]) -> dict[str, set[str]]:
    """Map every referenced page stem / id / name to the set of pages that mention it."""
    inv: dict[str, set[str]] = {}
    for page, refs in index.items():
        for kind in ("links", "ids", "names"):
            for x in refs.get(kind, []):
                inv.setdefault(x, set()).add(page)
    return inv


MIN_BARE_NAME_LEN = 5


def unique_names_of(graph: dict[str, Node], ids: set[str]) -> set[str]:
    """Bare names of ``ids`` that identify exactly one component in ``graph``.

    A mention like `update` could be any of several functions, so it is
    ignored; `update_all_packages` names one thing and counts.
    """
    counts: dict[str, int] = {}
    for node in graph.values():
        if node.name:
            counts[node.name] = counts.get(node.name, 0) + 1
    return {
        graph[c].name
        for c in ids
        if c in graph
        and graph[c].name
        and len(graph[c].name) >= MIN_BARE_NAME_LEN
        and counts.get(graph[c].name, 0) == 1
    }


def names_of(graph: dict[str, Node], ids: set[str]) -> set[str]:
    return {graph[c].name for c in ids if c in graph and graph[c].name and len(graph[c].name) >= 3}
