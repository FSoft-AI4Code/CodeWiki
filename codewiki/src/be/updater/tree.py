"""Helpers over ``module_tree.json``.

Shape: ``{name: {"path"?: str, "components": [ids], "children": {...}}}``.
A node is a leaf when ``children`` is missing or empty. Parents usually carry
the union of their children's components (super-grouping) or the set that
was later subdivided (clustering), so the *owner* of a component is the
deepest node that lists it. Agent-inserted sub-modules have no ``path`` key.

Paths are tuples of names from the root, e.g. ``("core", "auth")``.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node

Path = tuple[str, ...]


def is_leaf(info: dict[str, Any]) -> bool:
    children = info.get("children")
    return not children or (isinstance(children, dict) and len(children) == 0)


def components_of(info: dict[str, Any]) -> list[str]:
    comps = info.get("components")
    return list(comps) if isinstance(comps, list) else []


def iter_nodes(tree: dict[str, Any], prefix: Path = ()) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Pre-order walk yielding ``(path, info)`` for every node."""
    for name, info in tree.items():
        if not isinstance(info, dict):
            continue
        path = prefix + (name,)
        yield path, info
        children = info.get("children")
        if isinstance(children, dict) and children:
            yield from iter_nodes(children, path)


def iter_leaves(tree: dict[str, Any]) -> Iterator[tuple[Path, dict[str, Any]]]:
    for path, info in iter_nodes(tree):
        if is_leaf(info):
            yield path, info


def leaf_paths(tree: dict[str, Any]) -> list[Path]:
    return [p for p, _ in iter_leaves(tree)]


def preorder_paths(tree: dict[str, Any]) -> list[Path]:
    return [p for p, _ in iter_nodes(tree)]


def node_at(tree: dict[str, Any], path: Path) -> dict[str, Any] | None:
    level = tree
    info: dict[str, Any] | None = None
    for i, name in enumerate(path):
        if not isinstance(level, dict) or name not in level:
            return None
        info = level[name]
        if i < len(path) - 1:
            level = info.get("children", {})
    return info


def ancestors(path: Path) -> list[Path]:
    """Proper ancestors, nearest first: ``("a","b","c") -> [("a","b"), ("a",)]``."""
    return [path[:i] for i in range(len(path) - 1, 0, -1)]


def owner_map(tree: dict[str, Any]) -> dict[str, Path]:
    """Map each component id to the deepest node that lists it."""
    owner: dict[str, Path] = {}
    for path, info in iter_nodes(tree):
        for cid in components_of(info):
            prev = owner.get(cid)
            if prev is None or len(path) > len(prev):
                owner[cid] = path
    return owner


def resolve_owner(owner: dict[str, Path], cid: str) -> Path | None:
    """Owner of ``cid``, or of its enclosing class when ``cid`` itself is untracked.

    Clustering only places selected leaf nodes (mostly classes). A method
    ``path::Cls.m`` therefore has no owner of its own; it belongs to the leaf
    of ``path::Cls``. Nested classes resolve the same way, one dot at a time.
    """
    if cid in owner:
        return owner[cid]
    if "::" not in cid:
        return None
    path, name = cid.split("::", 1)
    while "." in name:
        name = name.rsplit(".", 1)[0]
        parent = f"{path}::{name}"
        if parent in owner:
            return owner[parent]
    return None


def owned_directly(tree: dict[str, Any]) -> dict[Path, list[str]]:
    """Components per node that no descendant lists (what a node really owns).

    For leaves this is their whole component list. A parent normally owns
    nothing directly; when it does, it must be treated as a leaf for those
    components (Part 13 of the redesign note).
    """
    owner = owner_map(tree)
    result: dict[Path, list[str]] = {}
    for path, info in iter_nodes(tree):
        mine = [cid for cid in components_of(info) if owner.get(cid) == path]
        if mine or is_leaf(info):
            result[path] = mine
    return result


def unit_paths(tree: dict[str, Any]) -> list[Path]:
    """Leaves plus parents that own components directly: the update units."""
    return list(owned_directly(tree).keys())


def tracked_ids(tree: dict[str, Any]) -> set[str]:
    return set(owner_map(tree).keys())


def add_component(tree: dict[str, Any], path: Path, cid: str) -> None:
    """Add ``cid`` to the node at ``path`` and to every ancestor that keeps a list."""
    info = node_at(tree, path)
    if info is None:
        raise KeyError(f"no module at {path}")
    comps = info.setdefault("components", [])
    if cid not in comps:
        comps.append(cid)
    for anc in ancestors(path):
        anc_info = node_at(tree, anc)
        if anc_info is None or "components" not in anc_info:
            continue
        if cid not in anc_info["components"]:
            anc_info["components"].append(cid)


def remove_component(tree: dict[str, Any], cid: str) -> list[Path]:
    """Remove ``cid`` from every node that lists it; return the touched paths."""
    touched: list[Path] = []
    for path, info in iter_nodes(tree):
        comps = info.get("components")
        if isinstance(comps, list) and cid in comps:
            info["components"] = [c for c in comps if c != cid]
            touched.append(path)
    return touched


def rename_component(tree: dict[str, Any], old_id: str, new_id: str) -> list[Path]:
    touched: list[Path] = []
    for path, info in iter_nodes(tree):
        comps = info.get("components")
        if isinstance(comps, list) and old_id in comps:
            info["components"] = [new_id if c == old_id else c for c in comps]
            touched.append(path)
    return touched


def prune_empty(tree: dict[str, Any]) -> list[Path]:
    """Drop leaves with no components, then parents left with no children and
    no components, recursively. Returns the removed paths (deepest first)."""
    removed: list[Path] = []

    def _prune(level: dict[str, Any], prefix: Path) -> None:
        for name in list(level.keys()):
            info = level[name]
            if not isinstance(info, dict):
                continue
            path = prefix + (name,)
            children = info.get("children")
            if isinstance(children, dict) and children:
                _prune(children, path)
            children = info.get("children")
            has_children = isinstance(children, dict) and len(children) > 0
            if not has_children and not components_of(info):
                del level[name]
                removed.append(path)

    _prune(tree, ())
    return removed


def insert_leaf(
    tree: dict[str, Any],
    parent: Path,
    name: str,
    components: list[str],
    path_hint: str | None = None,
) -> Path:
    """Create a new leaf ``name`` under ``parent`` (``()`` = top level)."""
    if parent:
        parent_info = node_at(tree, parent)
        if parent_info is None:
            raise KeyError(f"no module at {parent}")
        level = parent_info.setdefault("children", {})
    else:
        level = tree
    if name in level:
        raise KeyError(f"module {name!r} already exists under {parent}")
    info: dict[str, Any] = {"components": list(components), "children": {}}
    if path_hint:
        info["path"] = path_hint
    level[name] = info
    new_path = parent + (name,)
    for cid in components:
        add_component(tree, new_path, cid)
    return new_path


def reverse_edges(graph: dict[str, Node]) -> dict[str, set[str]]:
    """``in(c)``: the components whose ``depends_on`` contains ``c``."""
    rev: dict[str, set[str]] = {}
    for cid, node in graph.items():
        for dep in node.depends_on or ():
            rev.setdefault(dep, set()).add(cid)
    return rev


def leaf_dependents(
    tree: dict[str, Any], graph: dict[str, Node], owner: dict[str, Path] | None = None
) -> dict[Path, set[Path]]:
    """Lifted dependency: ``Dep(l)`` = units whose code *uses* code in ``l``."""
    owner = owner if owner is not None else owner_map(tree)
    dep: dict[Path, set[Path]] = {p: set() for p in unit_paths(tree)}
    for user, node in graph.items():
        user_leaf = resolve_owner(owner, user)
        if user_leaf is None:
            continue
        for used in node.depends_on or ():
            used_leaf = resolve_owner(owner, used)
            if used_leaf is not None and used_leaf != user_leaf:
                dep.setdefault(used_leaf, set()).add(user_leaf)
    return dep


def copy_tree(tree: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(tree)


def page_stem(path: Path) -> str:
    """Module page file stem (docs are flat: ``<name>.md``)."""
    return path[-1]


def virtual_whole_repo_tree(repo_name: str, tracked: list[str]) -> dict[str, Any]:
    """Whole-repository mode has an empty tree and one page. Model it as a
    single leaf holding every tracked component so any change activates it."""
    return {repo_name: {"components": list(tracked), "children": {}}}
