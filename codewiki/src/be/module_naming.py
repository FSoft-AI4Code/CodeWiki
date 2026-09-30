"""Utilities for keeping LLM-chosen module names unique and file-safe.

All module docs live in one flat directory as ``{module_name}.md``, and the
module-tree key must stay equal to the filename stem (the HTML viewer and
``--update`` invalidation rely on it). Names are chosen freely by the LLM at
every hierarchy level, so collisions must be resolved before a name is
inserted into the tree (issue #76).
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import logging

logger = logging.getLogger(__name__)

# Filename stems used by CodeWiki itself; never assign them to a module.
RESERVED_STEMS = {"overview", "module_tree", "first_module_tree", "metadata", "index"}

_UNSAFE_FILENAME_CHARS = set('/\\:*?"<>|\0')


def sanitize_module_name(name: str) -> str:
    """Make a module name safe to use as a filename stem (no case changes)."""
    cleaned = "".join("_" if c in _UNSAFE_FILENAME_CHARS else c for c in str(name).strip())
    cleaned = cleaned.replace(" ", "_").strip("._")
    return cleaned or "module"


def collect_module_tree_names(module_tree: Dict[str, Any]) -> Set[str]:
    """Collect all module names at every depth of the tree."""
    names = set()
    stack = [module_tree]
    while stack:
        level = stack.pop()
        if not isinstance(level, dict):
            continue
        for name, info in level.items():
            names.add(name)
            if isinstance(info, dict) and isinstance(info.get("children"), dict):
                stack.append(info["children"])
    return names


def resolve_unique_name(name: str, parent_name: Optional[str], taken: Set[str]) -> str:
    """Return ``name`` if free, else prefix with parent, else add a numeric suffix."""
    if name not in taken:
        return name
    if parent_name:
        prefixed = f"{sanitize_module_name(parent_name)}_{name}"
        if prefixed not in taken:
            return prefixed
    else:
        prefixed = name
    n = 2
    while f"{prefixed}_{n}" in taken:
        n += 1
    return f"{prefixed}_{n}"


def _existing_doc_stems(working_dir: str) -> Set[str]:
    try:
        return {
            os.path.splitext(entry)[0] for entry in os.listdir(working_dir) if entry.endswith(".md")
        }
    except OSError:
        return set()


def normalize_sub_module_specs(
    sub_module_specs: Dict[str, Any],
    parent_name: Optional[str],
    module_tree: Dict[str, Any],
    working_dir: str,
) -> Dict[str, str]:
    """Map requested sub-module names to unique, file-safe final names.

    A name is taken if it already appears anywhere in the module tree, if a
    ``.md`` with that stem exists in the flat docs dir, if it is reserved, or
    if it was assigned earlier in this batch.
    """
    taken = collect_module_tree_names(module_tree)
    taken |= _existing_doc_stems(working_dir)
    taken |= RESERVED_STEMS

    name_map: Dict[str, str] = {}
    for requested_name in sub_module_specs:
        final_name = resolve_unique_name(sanitize_module_name(requested_name), parent_name, taken)
        taken.add(final_name)
        name_map[requested_name] = final_name
        if final_name != requested_name:
            logger.info(
                "Sub-module name '%s' collides with an existing module or file; renamed to '%s'.",
                requested_name,
                final_name,
            )
    return name_map


@dataclass
class SubModulePlan:
    """What ``generate_sub_module_documentation`` will actually create.

    ``name_map``: requested name -> final, unique file stem to generate.
    ``skipped``: requested name -> reason it is not generated (already
    documented under that name or its parent-prefixed variant).
    """

    name_map: Dict[str, str] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)


def plan_sub_module_specs(
    sub_module_specs: Dict[str, Any],
    parent_name: Optional[str],
    module_tree: Dict[str, Any],
    working_dir: str,
) -> SubModulePlan:
    """Decide which requested sub-modules to generate (issue #113).

    Like :func:`normalize_sub_module_specs`, a name that collides with the
    tree, a ``.md`` on disk or a reserved stem gets the parent prefix. Unlike
    it, a request whose plain *and* prefixed names are both taken is treated
    as a repeat of something already documented and skipped, never renamed
    with a numeric suffix: that suffixing is what let one agent regenerate the
    same modules as ``x_2``, ``x_3``, ... without ever converging.
    """
    taken = collect_module_tree_names(module_tree)
    taken |= _existing_doc_stems(working_dir)
    taken |= RESERVED_STEMS

    plan = SubModulePlan()
    for requested_name in sub_module_specs:
        name = sanitize_module_name(requested_name)
        if name not in taken:
            final_name = name
        else:
            prefixed = f"{sanitize_module_name(parent_name)}_{name}" if parent_name else name
            if prefixed in taken:
                existing = name if name in taken else prefixed
                reason = f"already documented as {existing}.md; do not request it again"
                plan.skipped[requested_name] = reason
                logger.info(
                    "Sub-module '%s' already documented as '%s'; skipping duplicate request.",
                    requested_name,
                    existing,
                )
                continue
            final_name = prefixed
            logger.info(
                "Sub-module name '%s' collides with an existing module or file; renamed to '%s'.",
                requested_name,
                final_name,
            )
        taken.add(final_name)
        plan.name_map[requested_name] = final_name
    return plan


def dedupe_module_tree_names(module_tree: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize and uniquify all module names in a freshly clustered tree.

    Must only run on trees whose docs have not been generated yet — renaming
    a key whose ``.md`` already exists would orphan the doc.
    """
    taken: Set[str] = set(RESERVED_STEMS)

    def dedupe_level(level: Dict[str, Any], parent_name: Optional[str]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for name, info in level.items():
            final_name = resolve_unique_name(sanitize_module_name(name), parent_name, taken)
            taken.add(final_name)
            if final_name != name:
                logger.info("Module name '%s' collides; renamed to '%s'.", name, final_name)
            if isinstance(info, dict) and isinstance(info.get("children"), dict):
                info = {**info, "children": dedupe_level(info["children"], final_name)}
            result[final_name] = info
        return result

    return dedupe_level(module_tree, None)


def resolve_module_doc_path(working_dir: str, module_name: str) -> Optional[str]:
    """Resolve the on-disk path for a module's .md doc.

    Sub-agents sometimes save files under a sanitized variant of the module
    name (spaces → underscores, lowercased, etc.) rather than the exact key
    in the module tree. Try a small set of common variants before giving up.
    """
    candidates = []
    seen = set()
    base_variants = [
        module_name,
        module_name.replace(" ", "_"),
        module_name.replace(" ", "-"),
        module_name.replace(" ", ""),
    ]
    for variant in base_variants:
        for cased in (variant, variant.lower()):
            if cased not in seen:
                seen.add(cased)
                candidates.append(f"{cased}.md")

    for filename in candidates:
        candidate_path = os.path.join(working_dir, filename)
        if os.path.exists(candidate_path):
            return candidate_path
    return None


def find_missing_module_docs(
    module_tree: Dict[str, Any],
    working_dir: str,
    overview_required: bool = True,
) -> List[str]:
    """Return module names from the tree whose docs are missing on disk."""
    missing = []
    for name in sorted(collect_module_tree_names(module_tree)):
        if resolve_module_doc_path(working_dir, name) is None:
            missing.append(name)
    if overview_required and not os.path.exists(os.path.join(working_dir, "overview.md")):
        missing.append("overview")
    return missing


def _first_h1(path: str) -> Optional[str]:
    in_fence = False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(("```", "~~~")):
                    in_fence = not in_fence
                elif not in_fence and stripped.startswith("# "):
                    return stripped[2:].strip().strip("#").strip() or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def extract_page_titles(working_dir: str, module_tree: Dict[str, Any]) -> Dict[str, str]:
    """Map module names (and "overview") to the first `# ` heading of their page.

    Used as viewer display titles for docs written in another language, so the
    navigation can be translated while filenames stay equal to module names.
    Pages that are missing or have no H1 are left out.
    """
    titles = {}
    for name in ["overview", *sorted(collect_module_tree_names(module_tree))]:
        path = (
            os.path.join(working_dir, "overview.md")
            if name == "overview"
            else resolve_module_doc_path(working_dir, name)
        )
        title = _first_h1(path) if path and os.path.exists(path) else None
        if title:
            titles[name] = title
    return titles
