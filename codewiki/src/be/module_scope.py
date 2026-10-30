"""Language-independent module sizes, file coverage, and file ownership.

Core components are entry points, not a size estimate. A module owns files;
its size includes every indexed component in those files, including methods
which leaf selection intentionally omits. Parent lists may aggregate children,
but a file must have only one leaf owner.
"""

from __future__ import annotations

import logging
import posixpath
from collections import Counter, defaultdict
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.utils import count_tokens

logger = logging.getLogger(__name__)


def component_files(ids: list[str], components: dict[str, Node]) -> set[str]:
    return {components[cid].relative_path for cid in ids if cid in components}


def file_token_counts(components: dict[str, Node]) -> dict[str, int]:
    """Sum component source tokens, deliberately retaining overlapping source."""
    totals: dict[str, int] = defaultdict(int)
    for node in components.values():
        totals[node.relative_path] += count_tokens(node.source_code or "")
    return dict(totals)


def get_module_token_count(components: dict[str, Node], scope_files: set[str] | None = None) -> int:
    return sum(
        count_tokens(node.source_code or "")
        for node in components.values()
        if scope_files is None or node.relative_path in scope_files
    )


def complete_file_candidates(
    ids: list[str], components: dict[str, Node], scope_files: set[str] | None = None
) -> list[str]:
    """Keep entry points and add one stable representative per uncovered file."""
    scope = (
        {node.relative_path for node in components.values()} if scope_files is None else scope_files
    )
    selected = {cid for cid in ids if cid in components and components[cid].relative_path in scope}
    covered = component_files(list(selected), components)
    for cid in sorted(components):
        node = components[cid]
        path = node.relative_path
        # A supported source file may also have capped artifact entry points.
        # Retain its full-source representative for documentation as code.
        if path in scope and (path not in covered or node.component_type == "file"):
            selected.add(cid)
            covered.add(path)
    return sorted(selected, key=lambda cid: (components[cid].relative_path, cid))


def normalize_module_partition(
    proposed: dict[str, Any], candidates: list[str], components: dict[str, Node]
) -> dict[str, Any]:
    """Turn an untrusted flat proposal into a complete, disjoint file partition.

    Votes use distinct known IDs in the current file scope. All candidates of
    a file follow its winning group, including IDs omitted from the response.
    Unassigned files prefer the nearest directory, then the smallest group.
    An entirely unusable proposal returns {}, letting clustering use its
    deterministic structural fallback instead.
    """
    by_file: dict[str, list[str]] = defaultdict(list)
    for cid in candidates:
        if cid in components:
            by_file[components[cid].relative_path].append(cid)
    votes: dict[str, Counter] = defaultdict(Counter)
    for name, info in proposed.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            continue
        ids = info.get("components", [])
        if not isinstance(ids, list):
            continue
        for cid in set(cid for cid in ids if isinstance(cid, str)):
            if cid in components and components[cid].relative_path in by_file:
                votes[components[cid].relative_path][name] += 1
    if not votes:
        return {}

    missing_count = len(by_file.keys() - votes.keys())
    conflicting_count = sum(len(groups) > 1 for groups in votes.values())
    if missing_count or conflicting_count:
        logger.warning(
            "Reconciling module file assignments: %d omitted files, %d files assigned to multiple "
            "groups; restoring complete file ownership.",
            missing_count,
            conflicting_count,
        )

    assigned: dict[str, list[str]] = defaultdict(list)
    for path in sorted(votes):
        winner = min(votes[path], key=lambda name: (-votes[path][name], name))
        assigned[winner].append(path)
    sizes = file_token_counts(components)
    loads = {name: sum(sizes[path] for path in paths) for name, paths in assigned.items()}

    def proximity(left: str, right: str) -> int:
        left_parts = left.split("/")[:-1]
        right_parts = right.split("/")[:-1]
        depth = 0
        for a, b in zip(left_parts, right_parts):
            if a != b:
                break
            depth += 1
        return depth

    for path in sorted(by_file.keys() - votes.keys()):
        winner = min(
            assigned,
            key=lambda name: (
                -max(proximity(path, other) for other in assigned[name]),
                loads[name],
                name,
            ),
        )
        assigned[winner].append(path)
        loads[winner] += sizes[path]

    return {
        name: {
            "path": posixpath.commonpath(paths),
            "components": [cid for path in sorted(paths) for cid in sorted(set(by_file[path]))],
            "children": {},
        }
        for name, paths in assigned.items()
    }


def module_file_owners(tree: dict[str, Any], components: dict[str, Node]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = defaultdict(list)

    def walk(level: dict[str, Any], parent: tuple[str, ...]) -> None:
        for name, info in level.items():
            path = (*parent, name)
            children = info.get("children") or {}
            if children:
                walk(children, path)
            else:
                for file in component_files(info.get("components", []), components):
                    owners[file].append("/".join(path))

    walk(tree, ())
    return dict(owners)


def validate_module_file_coverage(
    tree: dict[str, Any],
    components: dict[str, Node],
    scope_files: set[str] | None = None,
) -> None:
    """Fail before saving a partial/overlapping tree; {} is intentional one-page mode."""
    if not tree:
        return
    expected = (
        scope_files if scope_files is not None else component_files(list(components), components)
    )
    owners = module_file_owners(tree, components)
    missing = sorted(expected - owners.keys())
    duplicates = {path: modules for path, modules in owners.items() if len(modules) > 1}
    outside = sorted(owners.keys() - expected)
    if missing or duplicates or outside:
        raise ValueError(
            f"Invalid module file coverage: missing={missing}, duplicate_owners={duplicates}, "
            f"outside_scope={outside}"
        )
    logger.info(
        "Module file coverage: %d/%d files owned, 0 missing, 0 duplicate owners",
        len(owners),
        len(expected),
    )


def prepare_submodule_partition(
    specs: dict[str, list[str]],
    parent_ids: list[str],
    components: dict[str, Node],
    existing_children: dict[str, Any],
) -> dict[str, list[str]]:
    """Partition the remaining parent files without stealing an existing child's files."""
    locked = set(module_file_owners(existing_children, components))
    scope = component_files(parent_ids, components) - locked
    candidates = complete_file_candidates(parent_ids, components, scope)
    normalized = normalize_module_partition(
        {name: {"components": ids} for name, ids in specs.items()}, candidates, components
    )
    return {name: info["components"] for name, info in normalized.items()}
