from typing import List, Dict, Any, Callable, Optional
from collections import defaultdict
import ast
import logging
import traceback
logger = logging.getLogger(__name__)

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm
from codewiki.src.be.module_naming import resolve_unique_name, sanitize_module_name
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import (
    Config,
    DEFAULT_MAX_LEAF_NODES_PER_CLUSTER,
    DEFAULT_MIN_MODULES_FOR_SUPER_GROUPING,
)
from codewiki.src.be.prompt_template import format_cluster_prompt, format_super_group_prompt

Completer = Callable[[str], Optional[str]]

# When whole-repo mode is chosen but leaf entry points touch fewer than this
# fraction of parsed files, warn that coverage depends on agent exploration.
LOW_COVERAGE_RATIO = 0.5


def format_potential_core_components(leaf_nodes: List[str], components: Dict[str, Node]) -> tuple[str, str]:
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
    
    #group leaf nodes by file
    leaf_nodes_by_file = defaultdict(list)
    for leaf_node in valid_leaf_nodes:
        leaf_nodes_by_file[components[leaf_node].relative_path].append(leaf_node)

    potential_core_components = ""
    potential_core_components_with_code = ""
    for file, leaf_nodes in dict(sorted(leaf_nodes_by_file.items())).items():
        potential_core_components += f"# {file}\n"
        potential_core_components_with_code += f"# {file}\n"
        for leaf_node in leaf_nodes:
            potential_core_components += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"{components[leaf_node].source_code}\n"

    return potential_core_components, potential_core_components_with_code


def get_clustering_input_token_count(
    leaf_nodes: List[str], components: Dict[str, Node]
) -> int:
    """Count the tokens used to decide whether a module needs clustering."""
    _, potential_core_components_with_code = format_potential_core_components(
        leaf_nodes, components
    )
    return count_tokens(potential_core_components_with_code)


def _cluster_batch_fits(node_ids: List[str], config: Config) -> bool:
    """Whether a single LLM clustering call can handle these nodes.

    The clustering response must re-emit every component ID verbatim, so the
    joined ID list is a direct proxy for output size; keep 2x headroom under
    max_tokens for dict syntax, module names/paths, and preamble.
    """
    max_nodes = getattr(
        config, "max_leaf_nodes_per_cluster", DEFAULT_MAX_LEAF_NODES_PER_CLUSTER
    )
    if len(node_ids) > max_nodes:
        return False
    output_budget = max(2048, config.max_tokens // 2)
    return count_tokens("\n".join(node_ids)) <= output_budget


def partition_leaf_nodes_by_structure(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    fits: Callable[[List[str]], bool],
) -> List[List[str]]:
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

    def path_parts(node: str) -> List[str]:
        return components[node].relative_path.strip("/").split("/")

    def chunk(nodes: List[str]) -> List[List[str]]:
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
        return [nodes[i:i + size] for i in range(0, len(nodes), size)]

    def split(nodes: List[str], depth: int) -> List[List[str]]:
        by_prefix = defaultdict(list)
        for node in nodes:
            by_prefix["/".join(path_parts(node)[:depth])].append(node)
        groups: List[List[str]] = []
        for prefix in sorted(by_prefix):
            sub = by_prefix[prefix]
            if fits(sub):
                groups.append(sub)
            elif any(len(path_parts(node)) > depth for node in sub):
                groups.extend(split(sub, depth + 1))
            else:
                groups.extend(chunk(sub))
        return groups

    batches: List[List[str]] = []
    current: List[str] = []
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
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: Dict[str, Any],
    current_module_name: Optional[str],
    module_label: str,
    completer: Optional[Completer],
) -> Dict[str, Any]:
    """Run one clustering LLM call over these nodes.

    Returns {} for any empty, malformed, or non-dict response instead of
    raising, so callers can fall back gracefully.
    """
    potential_core_components, _ = format_potential_core_components(leaf_nodes, components)
    prompt = format_cluster_prompt(potential_core_components, current_module_tree, current_module_name)
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

        response_content = response.split("<GROUPED_COMPONENTS>")[1].split("</GROUPED_COMPONENTS>")[0]
        module_tree = eval(response_content)

        if not isinstance(module_tree, dict):
            logger.error(f"Invalid module tree format - expected dict, got {type(module_tree)}")
            return {}

    except Exception as e:
        logger.warning(
            "Failed to parse LLM clustering response for %s; falling back. "
            "Error: %s. Response preview: %s...",
            module_label,
            e,
            str(response)[:200],
        )
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {}

    return module_tree


def _merge_module_trees(target: Dict[str, Any], addition: Dict[str, Any]) -> None:
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
            "Module '%s' was produced by multiple clustering batches; merged "
            "into %d components.",
            name,
            len(merged),
        )


def _batch_fallback_name(
    batch: List[str], components: Dict[str, Node], existing: Dict[str, Any]
) -> str:
    """Directory-derived module name for a batch whose LLM clustering failed."""
    prefix = _common_path_prefix(
        [components[node].relative_path for node in batch if node in components]
    )
    name = sanitize_module_name(prefix.replace("/", "_")) if prefix else "root"
    return resolve_unique_name(name, None, set(existing))


def cluster_modules(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any] = {},
    current_module_name: str = None,
    current_module_path: List[str] = [],
    completer: Optional[Completer] = None,
) -> Dict[str, Any]:
    """
    Cluster the potential core components into modules.

    Args:
        completer: optional ``(prompt: str) -> str`` callable.  When provided,
            clustering calls go through this completer instead of the legacy
            ``call_llm``.  This is how the LLMBackend abstraction injects
            subscription-mode (caw) routing.  If ``None``, falls back to
            ``call_llm`` for backward compatibility with direct callers.
    """
    _, potential_core_components_with_code = (
        format_potential_core_components(leaf_nodes, components)
    )
    input_tokens = count_tokens(potential_core_components_with_code)
    threshold = config.max_token_per_module
    module_label = current_module_name or "repository"

    logger.info(
        "Module clustering input for %s: %d leaf nodes, %d tokens, threshold %d",
        module_label,
        len(leaf_nodes),
        input_tokens,
        threshold,
    )

    if input_tokens <= threshold:
        logger.info(
            "Skipping LLM module clustering for %s because %d tokens fit within the "
            "%d-token threshold; using whole-module documentation mode.",
            module_label,
            input_tokens,
            threshold,
        )
        if current_module_name is None:
            leaf_files = {
                components[leaf_node].relative_path
                for leaf_node in leaf_nodes
                if leaf_node in components
            }
            all_files = {c.relative_path for c in components.values()}
            if all_files and len(leaf_files) / len(all_files) < LOW_COVERAGE_RATIO:
                logger.warning(
                    "Leaf-node entry points cover only %d of %d parsed files (%.0f%%). "
                    "Whole-repository documentation will start from these entry points and "
                    "rely on agent exploration to reach the rest of the codebase.",
                    len(leaf_files),
                    len(all_files),
                    100 * len(leaf_files) / len(all_files),
                )
        return {}

    logger.info(
        "Requesting LLM module clustering for %s because %d tokens exceed the %d-token threshold.",
        module_label,
        input_tokens,
        threshold,
    )

    batches = partition_leaf_nodes_by_structure(
        leaf_nodes, components, lambda ids: _cluster_batch_fits(ids, config)
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
        if not module_tree:
            return {}
    else:
        logger.info(
            "Partitioned %d leaf nodes for %s into %d structure-based batches for clustering.",
            len(leaf_nodes),
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

    # check if the module tree is valid
    if len(module_tree) <= 1:
        logger.info(
            "Skipping LLM clustering result for %s because it produced only "
            "%d module(s); using whole-module documentation mode.",
            module_label,
            len(module_tree),
        )
        return {}

    logger.info(
        "LLM module clustering for %s produced %d top-level modules.",
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
        sub_leaf_nodes = module_info.get("components", [])
        
        # Filter sub_leaf_nodes to ensure they exist in components
        valid_sub_leaf_nodes = []
        for node in sub_leaf_nodes:
            if node in components:
                valid_sub_leaf_nodes.append(node)
            else:
                logger.warning(f"Skipping invalid sub leaf node '{node}' in module '{module_name}' - not found in components")
        
        current_module_path.append(module_name)
        module_info["children"] = {}
        module_info["children"] = cluster_modules(
            valid_sub_leaf_nodes,
            components,
            config,
            current_module_tree,
            module_name,
            current_module_path,
            completer=completer,
        )
        current_module_path.pop()

    return module_tree


def _common_path_prefix(paths: List[str]) -> str:
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


def _parse_super_group_response(response: Optional[str]) -> Optional[Dict[str, Any]]:
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
    except Exception as e:
        logger.warning(
            "Failed to parse super-grouping response; keeping the flat module "
            "tree. Error: %s. Response preview: %s...",
            e,
            str(response)[:200],
        )
        return None
    if not isinstance(grouping, dict):
        logger.warning(
            "Invalid super-grouping format - expected dict, got %s; keeping the "
            "flat module tree.",
            type(grouping),
        )
        return None
    return grouping


def super_group_modules(
    module_tree: Dict[str, Any],
    config: Config,
    completer: Optional[Completer] = None,
) -> Dict[str, Any]:
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
        logger.info(
            "Super-grouping disabled (min_modules_for_super_grouping=%d).", min_modules
        )
        return module_tree
    if len(module_tree) <= min_modules:
        logger.info(
            "Skipping super-grouping: %d top-level modules fit within the "
            "%d-module threshold.",
            len(module_tree),
            min_modules,
        )
        return module_tree

    prompt = format_super_group_prompt(module_tree)
    logger.info(
        "Requesting super-grouping of %d top-level modules into architectural "
        "subsystems.",
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
    subsystems: Dict[str, List[str]] = {}
    for subsystem_name, info in grouping.items():
        members = info.get("modules") if isinstance(info, dict) else None
        if not isinstance(members, list):
            logger.warning(
                "Skipping subsystem '%s' in super-grouping response: no valid "
                "'modules' list.",
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
                    "Module '%s' assigned to multiple subsystems; keeping its "
                    "first assignment.",
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

    result: Dict[str, Any] = {}
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
        "Super-grouping consolidated %d top-level modules into %d entries "
        "(%d subsystems).",
        len(module_tree),
        len(result),
        len(subsystems),
    )
    return result
