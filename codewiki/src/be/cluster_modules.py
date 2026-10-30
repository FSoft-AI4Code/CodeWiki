import ast
import logging
import traceback
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm
from codewiki.src.be.module_naming import resolve_unique_name, sanitize_module_name
from codewiki.src.be.module_scope import (
    complete_file_candidates,
    component_files,
    file_token_counts,
    get_module_token_count,
    normalize_module_partition,
    validate_module_file_coverage,
)
from codewiki.src.be.prompt_template import format_cluster_prompt, format_super_group_prompt
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import (
    DEFAULT_MAX_LEAF_NODES_PER_CLUSTER,
    DEFAULT_MIN_MODULES_FOR_SUPER_GROUPING,
    Config,
)

logger = logging.getLogger(__name__)

Completer = Callable[[str], str | None]

# Below this share, log why entry points were expanded before choosing a mode.
LOW_COVERAGE_RATIO = 0.5


def format_potential_core_components(
    leaf_nodes: list[str], components: dict[str, Node]
) -> tuple[str, str]:
    """
    Format the potential core components into a string that can be used in the prompt.
    """
    # Filter out any invalid leaf nodes that don't exist in components
    valid_leaf_nodes = []
    for leaf_node in leaf_nodes:
        if leaf_node in components:
            valid_leaf_nodes.append(leaf_node)
        else:
            logger.warning(f"Skipping invalid leaf node '{leaf_node}' - not found in components")

    # group leaf nodes by file
    leaf_nodes_by_file = defaultdict(list)
    for leaf_node in valid_leaf_nodes:
        leaf_nodes_by_file[components[leaf_node].relative_path].append(leaf_node)

    potential_core_components = ""
    potential_core_components_with_code = ""
    for file, file_nodes in dict(sorted(leaf_nodes_by_file.items())).items():
        header = f"# {file}"
        if all(components[n].component_type == "artifact" for n in file_nodes):
            header += f" (artifact: {components[file_nodes[0]].artifact_class or 'config'})"
        potential_core_components += f"{header}\n"
        potential_core_components_with_code += f"{header}\n"
        for leaf_node in file_nodes:
            potential_core_components += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"{components[leaf_node].source_code}\n"

    return potential_core_components, potential_core_components_with_code


def get_clustering_input_token_count(leaf_nodes: list[str], components: dict[str, Node]) -> int:
    """Count leaf prompt tokens for diagnostics, never for the split decision."""
    _, potential_core_components_with_code = format_potential_core_components(
        leaf_nodes, components
    )
    return count_tokens(potential_core_components_with_code)


def _cluster_batch_fits(node_ids: list[str], config: Config) -> bool:
    """Whether a single LLM clustering call can handle these nodes.

    The clustering response must re-emit every component ID verbatim, so the
    joined ID list is a direct proxy for output size; keep 2x headroom under
    max_tokens for dict syntax, module names/paths, and preamble.
    """
    max_nodes = getattr(config, "max_leaf_nodes_per_cluster", DEFAULT_MAX_LEAF_NODES_PER_CLUSTER)
    if len(node_ids) > max_nodes:
        return False
    output_budget = max(2048, config.max_tokens // 2)
    return count_tokens("\n".join(node_ids)) <= output_budget


def partition_leaf_nodes_by_structure(
    leaf_nodes: list[str],
    components: dict[str, Node],
    fits: Callable[[list[str]], bool],
) -> list[list[str]]:
    """Partition leaf nodes into batches that each satisfy ``fits``.

    Splits along the directory structure of the nodes' relative paths, then
    greedily coalesces path-adjacent small groups so root-level files and tiny
    directories don't become their own batches. Deterministic: nodes are
    processed in (relative_path, id) order. Returns the input as a single
    batch when it already fits.
    """
    valid = []
    for leaf_node in leaf_nodes:
        if leaf_node in components:
            valid.append(leaf_node)
        else:
            logger.warning(f"Skipping invalid leaf node '{leaf_node}' - not found in components")
    valid.sort(key=lambda node: (components[node].relative_path, node))
    if not valid or fits(valid):
        return [valid]

    def path_parts(node: str) -> list[str]:
        return components[node].relative_path.strip("/").split("/")

    def chunk(nodes: list[str]) -> list[list[str]]:
        # Nodes that share one directory/file and still don't fit can only be
        # cut into fixed-size slices.
        size = len(nodes)
        while size > 1 and not fits(nodes[:size]):
            size = (size + 1) // 2
        logger.warning(
            "Splitting %d leaf nodes that share one path into slices of at "
            "most %d; directory structure can't divide them further.",
            len(nodes),
            size,
        )
        return [nodes[i : i + size] for i in range(0, len(nodes), size)]

    def split(nodes: list[str], depth: int) -> list[list[str]]:
        by_prefix = defaultdict(list)
        for node in nodes:
            by_prefix["/".join(path_parts(node)[:depth])].append(node)
        groups: list[list[str]] = []
        for prefix in sorted(by_prefix):
            sub = by_prefix[prefix]
            if fits(sub):
                groups.append(sub)
            elif any(len(path_parts(node)) > depth for node in sub):
                groups.extend(split(sub, depth + 1))
            else:
                groups.extend(chunk(sub))
        return groups

    batches: list[list[str]] = []
    current: list[str] = []
    for group in split(valid, 1):
        if not current:
            current = group
        elif fits(current + group):
            current.extend(group)
        else:
            batches.append(current)
            current = group
    if current:
        batches.append(current)
    return batches


def _cluster_via_llm(
    leaf_nodes: list[str],
    components: dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any],
    current_module_name: str | None,
    module_label: str,
    completer: Completer | None,
) -> dict[str, Any]:
    """Run one clustering LLM call over these nodes.

    Returns {} for any empty, malformed, or non-dict response instead of
    raising, so callers can fall back gracefully.
    """
    potential_core_components, _ = format_potential_core_components(leaf_nodes, components)
    prompt = format_cluster_prompt(
        potential_core_components, current_module_tree, current_module_name
    )
    if completer is not None:
        response = completer(prompt)
    else:
        response = call_llm(prompt, config, model=config.cluster_model)

    if not response:
        logger.warning(
            "Empty LLM clustering response for %s (provider returned no "
            "content, likely output truncation); falling back.",
            module_label,
        )
        return {}

    try:
        if "<GROUPED_COMPONENTS>" not in response or "</GROUPED_COMPONENTS>" not in response:
            logger.warning(
                "Invalid LLM clustering response for %s: missing <GROUPED_COMPONENTS> "
                "tags; falling back. Response preview: %s...",
                module_label,
                str(response)[:200],
            )
            return {}

        response_content = response.split("<GROUPED_COMPONENTS>")[1].split("</GROUPED_COMPONENTS>")[
            0
        ]
        module_tree = ast.literal_eval(response_content)

        if not isinstance(module_tree, dict):
            logger.error(f"Invalid module tree format - expected dict, got {type(module_tree)}")
            return {}

    except Exception as e:  # noqa: BLE001 — a failed LLM call must not abort clustering
        logger.warning(
            "Failed to parse LLM clustering response for %s; falling back. "
            "Error: %s. Response preview: %s...",
            module_label,
            e,
            str(response)[:200],
        )
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {}

    # Validate each batch before merging untrusted LLM output. File ownership
    # is reconciled again across batches once every response is available.
    return normalize_module_partition(module_tree, leaf_nodes, components)


def _merge_module_trees(target: dict[str, Any], addition: dict[str, Any]) -> None:
    """Merge one batch's module dict into the accumulated tree in place.

    Batches cluster independently, so two of them may propose the same module
    name for the same concept; merge their components rather than renaming
    (dedupe_module_tree_names still guards anything unexpected later).
    """
    for name, info in addition.items():
        if name not in target:
            target[name] = info
            continue
        existing = target[name]
        if not isinstance(existing, dict) or not isinstance(info, dict):
            logger.warning(
                "Cannot merge module '%s' from another batch; keeping the first version.",
                name,
            )
            continue
        seen = set(existing.get("components", []))
        merged = list(existing.get("components", []))
        for component in info.get("components", []):
            if component not in seen:
                seen.add(component)
                merged.append(component)
        existing["components"] = merged
        paths = [existing.get("path", ""), info.get("path", "")]
        existing["path"] = _common_path_prefix(paths) if all(paths) else ""
        logger.info(
            "Module '%s' was produced by multiple clustering batches; merged into %d components.",
            name,
            len(merged),
        )


def _batch_fallback_name(
    batch: list[str], components: dict[str, Node], existing: dict[str, Any]
) -> str:
    """Directory-derived module name for a batch whose LLM clustering failed."""
    prefix = _common_path_prefix(
        [components[node].relative_path for node in batch if node in components]
    )
    name = sanitize_module_name(prefix.replace("/", "_")) if prefix else "root"
    return resolve_unique_name(name, None, set(existing))


def _structural_fallback(
    candidates: list[str], components: dict[str, Node], threshold: int
) -> dict[str, Any]:
    """Pack path-adjacent whole files by code size when the LLM cannot split."""
    by_file = defaultdict(list)
    for cid in candidates:
        by_file[components[cid].relative_path].append(cid)
    sizes = file_token_counts(components)
    groups = []
    current: list[str] = []
    current_tokens = 0
    for path in sorted(by_file):
        if current and current_tokens + sizes[path] > threshold:
            groups.append(current)
            current = []
            current_tokens = 0
        current.extend(by_file[path])
        current_tokens += sizes[path]
    if current:
        groups.append(current)
    tree = {}
    for group in groups:
        name = _batch_fallback_name(group, components, tree)
        tree[name] = {
            "path": _common_path_prefix(sorted(component_files(group, components))),
            "components": group,
            "children": {},
        }
    return tree


def cluster_modules(
    leaf_nodes: list[str],
    components: dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any] | None = None,
    current_module_name: str | None = None,
    current_module_path: list[str] | None = None,
    completer: Completer | None = None,
    *,
    scope_files: set[str] | None = None,
) -> dict[str, Any]:
    """
    Cluster the potential core components into modules.

    Args:
        completer: optional ``(prompt: str) -> str`` callable.  When provided,
            clustering calls go through this completer instead of the legacy
            ``call_llm``.  This is how the LLMBackend abstraction injects
            subscription-mode (caw) routing.  If ``None``, falls back to
            ``call_llm`` for backward compatibility with direct callers.
        scope_files: exact file ownership of this invocation. The repository
            defaults to all indexed files; a submodule defaults to the files
            of its supplied entry points. Recursion always passes it explicitly.
    """
    current_module_tree = {} if current_module_tree is None else current_module_tree
    current_module_path = [] if current_module_path is None else current_module_path
    if scope_files is None:
        scope_files = (
            component_files(list(components), components)
            if current_module_name is None
            else component_files(leaf_nodes, components)
        )
    leaves = [
        cid
        for cid in leaf_nodes
        if cid in components and components[cid].relative_path in scope_files
    ]
    candidates = complete_file_candidates(leaves, components, scope_files)
    input_tokens = get_module_token_count(components, scope_files)
    leaf_tokens = get_clustering_input_token_count(leaves, components)
    leaf_files = component_files(leaves, components)
    coverage = len(leaf_files) / len(scope_files) if scope_files else 1.0
    threshold = config.max_token_per_module
    module_label = current_module_name or "repository"

    logger.info(
        "Module clustering input for %s: code_tokens=%d, leaf_tokens=%d, threshold=%d, "
        "leaf_files=%d/%d (%.0f%%), candidate_files=%d/%d, candidates=%d",
        module_label,
        input_tokens,
        leaf_tokens,
        threshold,
        len(leaf_files),
        len(scope_files),
        coverage * 100,
        len(component_files(candidates, components)),
        len(scope_files),
        len(candidates),
    )
    if coverage < LOW_COVERAGE_RATIO:
        logger.warning(
            "Leaf coverage for %s is below %.0f%%; added file representatives before "
            "choosing documentation mode (%d -> %d entry points).",
            module_label,
            LOW_COVERAGE_RATIO * 100,
            len(leaves),
            len(candidates),
        )

    if input_tokens <= threshold:
        logger.info(
            "Skipping LLM module clustering for %s because %d code tokens fit within the "
            "%d-token threshold; using whole-module documentation mode.",
            module_label,
            input_tokens,
            threshold,
        )
        return {}

    if len(scope_files) <= 1:
        logger.warning(
            "Module %s has %d code tokens above threshold %d but only %d file; "
            "keeping the indivisible file without another clustering call.",
            module_label,
            input_tokens,
            threshold,
            len(scope_files),
        )
        return {}

    logger.info(
        "Requesting LLM module clustering for %s because %d code tokens exceed the %d-token threshold.",
        module_label,
        input_tokens,
        threshold,
    )

    batches = partition_leaf_nodes_by_structure(
        candidates, components, lambda ids: _cluster_batch_fits(ids, config)
    )

    if len(batches) == 1:
        module_tree = _cluster_via_llm(
            batches[0],
            components,
            config,
            current_module_tree,
            current_module_name,
            module_label,
            completer,
        )
    else:
        logger.info(
            "Partitioned %d entry points for %s into %d structure-based batches for clustering.",
            len(candidates),
            module_label,
            len(batches),
        )
        module_tree = {}
        for i, batch in enumerate(batches, 1):
            batch_label = f"{module_label} (batch {i}/{len(batches)})"
            # Each batch gets the *unmodified* current_module_tree: passing the
            # accumulating merge would flip format_cluster_prompt into its
            # module-level variant mid-partition.
            partial = _cluster_via_llm(
                batch,
                components,
                config,
                current_module_tree,
                current_module_name,
                batch_label,
                completer,
            )
            if not partial:
                name = _batch_fallback_name(batch, components, module_tree)
                partial = {
                    name: {
                        "path": _common_path_prefix(
                            [components[n].relative_path for n in batch if n in components]
                        ),
                        "components": list(batch),
                    }
                }
                logger.warning(
                    "Clustering failed for %s; keeping its %d components as fallback module '%s'.",
                    batch_label,
                    len(batch),
                    name,
                )
            _merge_module_trees(module_tree, partial)

    module_tree = normalize_module_partition(module_tree, candidates, components)
    if len(module_tree) <= 1:
        logger.warning(
            "Clustering %s produced only %d usable module(s); using a structural file partition.",
            module_label,
            len(module_tree),
        )
        module_tree = _structural_fallback(candidates, components, threshold)

    logger.info(
        "Module clustering for %s produced %d top-level modules.",
        module_label,
        len(module_tree),
    )

    if current_module_tree == {}:
        current_module_tree = module_tree
    else:
        value = current_module_tree
        for key in current_module_path:
            value = value[key]["children"]
        for module_name, module_info in module_tree.items():
            module_info.pop("path", None)
            value[module_name] = module_info

    for module_name, module_info in module_tree.items():
        sub_files = component_files(module_info["components"], components)
        # Reuse original leaves for coverage diagnostics, not the added file
        # representatives. The next call reconstructs its complete candidate set.
        sub_leaves = [cid for cid in leaves if components[cid].relative_path in sub_files]

        current_module_path.append(module_name)
        module_info["children"] = {}
        module_info["children"] = cluster_modules(
            sub_leaves,
            components,
            config,
            current_module_tree,
            module_name,
            current_module_path,
            completer=completer,
            scope_files=sub_files,
        )
        current_module_path.pop()

    validate_module_file_coverage(module_tree, components, scope_files)
    return module_tree


def _common_path_prefix(paths: list[str]) -> str:
    """Longest common directory prefix of the given relative paths."""
    split_paths = [p.strip("/").split("/") for p in paths if p]
    if not split_paths:
        return ""
    common = []
    for parts in zip(*split_paths):
        if all(part == parts[0] for part in parts):
            common.append(parts[0])
        else:
            break
    return "/".join(common)


def _parse_super_group_response(response: str | None) -> dict[str, Any] | None:
    if not response:
        logger.warning(
            "Empty super-grouping response (provider returned no content, "
            "likely output truncation); keeping the flat module tree."
        )
        return None
    if "<GROUPED_MODULES>" not in response or "</GROUPED_MODULES>" not in response:
        logger.warning(
            "Invalid super-grouping response: missing <GROUPED_MODULES> tags; "
            "keeping the flat module tree. Response preview: %s...",
            str(response)[:200],
        )
        return None
    try:
        grouping = ast.literal_eval(
            response.split("<GROUPED_MODULES>")[1].split("</GROUPED_MODULES>")[0]
        )
    except Exception as e:  # noqa: BLE001 — a failed LLM call must not abort clustering
        logger.warning(
            "Failed to parse super-grouping response; keeping the flat module "
            "tree. Error: %s. Response preview: %s...",
            e,
            str(response)[:200],
        )
        return None
    if not isinstance(grouping, dict):
        logger.warning(
            "Invalid super-grouping format - expected dict, got %s; keeping the flat module tree.",
            type(grouping),
        )
        return None
    return grouping


def super_group_modules(
    module_tree: dict[str, Any],
    config: Config,
    completer: Completer | None = None,
) -> dict[str, Any]:
    """
    Group a flat top level of modules into higher-level architectural subsystems.

    Runs one extra LLM pass over the top-level module names (with their paths
    and components) and nests the flat modules as children of the subsystems it
    proposes. Any invalid or non-consolidating response leaves the tree
    unchanged, so this pass can never make the structure worse.
    """
    min_modules = getattr(
        config, "min_modules_for_super_grouping", DEFAULT_MIN_MODULES_FOR_SUPER_GROUPING
    )
    if min_modules <= 0:
        logger.info("Super-grouping disabled (min_modules_for_super_grouping=%d).", min_modules)
        return module_tree
    if len(module_tree) <= min_modules:
        logger.info(
            "Skipping super-grouping: %d top-level modules fit within the %d-module threshold.",
            len(module_tree),
            min_modules,
        )
        return module_tree

    prompt = format_super_group_prompt(module_tree)
    logger.info(
        "Requesting super-grouping of %d top-level modules into architectural subsystems.",
        len(module_tree),
    )
    if completer is not None:
        response = completer(prompt)
    else:
        response = call_llm(prompt, config, model=config.cluster_model)

    grouping = _parse_super_group_response(response)
    if grouping is None:
        return module_tree

    # Validate assignments: unknown modules are dropped, duplicates keep their
    # first assignment, unassigned modules stay at the top level.
    assigned = set()
    subsystems: dict[str, list[str]] = {}
    for subsystem_name, info in grouping.items():
        members = info.get("modules") if isinstance(info, dict) else None
        if not isinstance(members, list):
            logger.warning(
                "Skipping subsystem '%s' in super-grouping response: no valid 'modules' list.",
                subsystem_name,
            )
            continue
        valid_members = []
        for member in members:
            if member not in module_tree:
                logger.warning(
                    "Skipping unknown module '%s' in subsystem '%s'.",
                    member,
                    subsystem_name,
                )
                continue
            if member in assigned:
                logger.warning(
                    "Module '%s' assigned to multiple subsystems; keeping its first assignment.",
                    member,
                )
                continue
            assigned.add(member)
            valid_members.append(member)
        if valid_members:
            subsystems[subsystem_name] = valid_members

    unassigned = [name for name in module_tree if name not in assigned]

    # A single-module subsystem is just a rename; keep the module itself.
    top_level_count = len(subsystems) + len(unassigned)
    if len(subsystems) <= 1 or top_level_count >= len(module_tree):
        logger.info(
            "Skipping super-grouping result: %d subsystem(s) over %d modules "
            "provide no real consolidation.",
            len(subsystems),
            len(module_tree),
        )
        return module_tree

    result: dict[str, Any] = {}
    for subsystem_name, members in subsystems.items():
        if len(members) == 1:
            result[members[0]] = module_tree[members[0]]
            continue
        components = []
        seen = set()
        for member in members:
            for component in module_tree[member].get("components", []):
                if component not in seen:
                    seen.add(component)
                    components.append(component)
        result[subsystem_name] = {
            "path": _common_path_prefix(
                [module_tree[member].get("path", "") for member in members]
            ),
            "components": components,
            "children": {member: module_tree[member] for member in members},
        }
    for name in unassigned:
        result[name] = module_tree[name]

    logger.info(
        "Super-grouping consolidated %d top-level modules into %d entries (%d subsystems).",
        len(module_tree),
        len(result),
        len(subsystems),
    )
    return result


# --------------------------------------------------------------------------- #
# Guaranteed artifact module
# --------------------------------------------------------------------------- #

ARTIFACT_MODULE_NAME = "Build, Deployment and Configuration"
# Insert the fallback module when clustering kept less than this share of the
# artifact leaf nodes.
ARTIFACT_MIN_SHARE = 0.8


def collect_module_tree_component_ids(module_tree: dict[str, Any]) -> set:
    """Return every component id referenced anywhere in ``module_tree``."""
    ids: set = set()

    def _walk(tree: dict[str, Any]) -> None:
        for module_info in tree.values():
            if not isinstance(module_info, dict):
                continue
            ids.update(module_info.get("components", []) or [])
            children = module_info.get("children", {})
            if isinstance(children, dict):
                _walk(children)

    _walk(module_tree)
    return ids


def ensure_artifact_module(
    module_tree: dict[str, Any],
    leaf_nodes: list[str],
    components: dict[str, Node],
    min_share: float = ARTIFACT_MIN_SHARE,
) -> dict[str, Any]:
    """Guarantee that artifact leaf nodes are documented.

    Clustering is an LLM call and may drop or scatter artifact nodes despite
    the prompt. If fewer than ``min_share`` of the artifact leaf nodes landed
    in ``module_tree``, add a fixed top-level module holding every unassigned
    artifact node. Returns ``module_tree`` unchanged in whole-repository mode
    (empty tree: one agent documents all leaf nodes anyway).
    """
    if not module_tree:
        return module_tree
    artifact_leaves = [
        n for n in leaf_nodes if n in components and components[n].component_type == "artifact"
    ]
    if not artifact_leaves:
        return module_tree
    assigned = collect_module_tree_component_ids(module_tree)
    unassigned = [n for n in artifact_leaves if n not in assigned]
    share = 1.0 - len(unassigned) / len(artifact_leaves)
    logger.info(
        "Artifact coverage after clustering: %d/%d artifact leaf nodes assigned (%.0f%%)",
        len(artifact_leaves) - len(unassigned),
        len(artifact_leaves),
        share * 100,
    )
    if not unassigned or share >= min_share:
        return module_tree

    from codewiki.src.be.dependency_analyzer.analyzers.artifact import CLASS_PRIORITY
    from codewiki.src.be.module_naming import collect_module_tree_names

    def _order(node_id: str):
        node = components[node_id]
        cls = node.artifact_class or "config"
        rank = CLASS_PRIORITY.index(cls) if cls in CLASS_PRIORITY else len(CLASS_PRIORITY)
        return (rank, node.relative_path, node_id)

    unassigned.sort(key=_order)
    name = resolve_unique_name(ARTIFACT_MODULE_NAME, None, collect_module_tree_names(module_tree))
    module_tree[name] = {
        "path": _common_path_prefix([components[n].relative_path for n in unassigned]) or ".",
        "components": unassigned,
        "children": {},
    }
    logger.info(
        "Artifact coverage %.0f%% < %.0f%%; inserted top-level module '%s' with %d components",
        share * 100,
        min_share * 100,
        name,
        len(unassigned),
    )
    return module_tree
