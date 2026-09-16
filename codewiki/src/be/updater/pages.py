"""Small helpers over the flat docs directory (page stems <-> files, hashes)."""

from __future__ import annotations

import hashlib
import os

from codewiki.src.config import OVERVIEW_FILENAME

OVERVIEW_STEM = OVERVIEW_FILENAME[: -len(".md")]


def page_path(docs_dir: str, stem: str) -> str:
    return os.path.join(docs_dir, f"{stem}.md")


def page_exists(docs_dir: str, stem: str) -> bool:
    return os.path.isfile(page_path(docs_dir, stem))


def read_page(docs_dir: str, stem: str) -> str | None:
    try:
        with open(page_path(docs_dir, stem), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def list_pages(docs_dir: str) -> list[str]:
    try:
        return sorted(
            f[:-3] for f in os.listdir(docs_dir) if f.endswith(".md") and not f.startswith(".")
        )
    except OSError:
        return []


def page_hashes(docs_dir: str) -> dict[str, str]:
    out = {}
    for stem in list_pages(docs_dir):
        try:
            with open(page_path(docs_dir, stem), "rb") as f:
                out[stem] = hashlib.sha1(f.read()).hexdigest()
        except OSError:
            continue
    return out


def changed_pages(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Pages created, removed, or whose bytes changed."""
    return {s for s in set(before) | set(after) if before.get(s) != after.get(s)}
