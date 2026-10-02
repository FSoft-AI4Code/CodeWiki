"""Small helpers over the docs directory (page stems <-> files, hashes).

Pages are addressed by stem (the module name). Where the file lives depends
on the docs layout recorded in ``metadata.json`` (see ``doc_layout``): an
existing page is found in either layout, a new one is placed where the
recorded layout expects it.
"""

from __future__ import annotations

import hashlib
import os

from codewiki.src.be import doc_layout as L
from codewiki.src.config import OVERVIEW_FILENAME

OVERVIEW_STEM = OVERVIEW_FILENAME[: -len(".md")]


def page_path(docs_dir: str, stem: str) -> str:
    tree = L.load_module_tree(docs_dir)
    found = L.find_doc(docs_dir, stem, tree, search=True)
    if found is not None:
        return found
    return L.target_doc_path(docs_dir, stem, L.read_layout(docs_dir), tree)


def page_rel(docs_dir: str, stem: str) -> str:
    """Docs-relative POSIX path of the page (what agents pass to the editor)."""
    return os.path.relpath(page_path(docs_dir, stem), docs_dir).replace(os.sep, "/")


def page_exists(docs_dir: str, stem: str) -> bool:
    return os.path.isfile(page_path(docs_dir, stem))


def read_page(docs_dir: str, stem: str) -> str | None:
    try:
        with open(page_path(docs_dir, stem), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def list_pages(docs_dir: str) -> list[str]:
    return sorted(L.list_doc_files(docs_dir))


def page_hashes(docs_dir: str) -> dict[str, str]:
    out = {}
    for stem, rel in L.list_doc_files(docs_dir).items():
        try:
            with open(os.path.join(docs_dir, rel), "rb") as f:
                out[stem] = hashlib.sha1(f.read()).hexdigest()
        except OSError:
            continue
    return out


def remove_page(docs_dir: str, stem: str) -> bool:
    """Delete the page (and any folder left empty); False when it did not exist."""
    path = page_path(docs_dir, stem)
    if not os.path.isfile(path):
        return False
    L.remove_doc(docs_dir, path)
    return True


def changed_pages(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Pages created, removed, or whose bytes changed."""
    return {s for s in set(before) | set(after) if before.get(s) != after.get(s)}
