from typing import Dict, List, Set

from codewiki.src.be.dependency_analyzer.models.core import Node

import logging
logger = logging.getLogger(__name__)

# Below this many class/interface/struct components, a repo is considered
# barely-OOP and free functions dominate the architecture (issue #75).
MIN_OOP_COMPONENTS = 20
# When class/interface/struct components are less than this fraction of
# (OOP + function) components, functions carry most of the design.
OOP_MINORITY_RATIO = 0.2
# Above this many candidate leaf nodes, prune to true graph leaves.
LEAF_REDUCTION_THRESHOLD = 400

OOP_TYPES = {"class", "interface", "struct"}


def compute_valid_leaf_types(components: Dict[str, Node]) -> Set[str]:
    """
    Determine which component types qualify as leaf nodes.

    Classes/interfaces/structs always qualify. Free functions qualify when
    they carry the architecture: pure-C style repos with no OOP components,
    or mixed repos (C/C++/Go) where OOP components are a small minority.
    OOP-heavy repos keep the class-only behavior so node counts stay bounded.
    """
    n_oop = 0
    n_func = 0
    for comp in components.values():
        if comp.component_type in OOP_TYPES:
            n_oop += 1
        elif comp.component_type == "function":
            n_func += 1

    valid_types = set(OOP_TYPES)
    include_functions = (
        n_oop == 0
        or (n_oop < MIN_OOP_COMPONENTS and n_func > n_oop)
        or (n_func > 0 and n_oop / (n_oop + n_func) < OOP_MINORITY_RATIO)
    )
    if include_functions:
        valid_types.add("function")
        if n_oop > 0:
            logger.info(
                "Including function components as leaf candidates: %d class/interface/struct "
                "components vs %d functions — functions carry most of this codebase.",
                n_oop,
                n_func,
            )
    return valid_types


def filter_leaf_nodes(
    leaf_nodes,
    components: Dict[str, Node],
    valid_types: Set[str],
) -> List[str]:
    """Keep leaf nodes that are known components of a valid type."""
    keep_leaf_nodes = []
    for leaf_node in leaf_nodes:
        # Skip any leaf nodes that are clearly error strings or invalid identifiers
        if not isinstance(leaf_node, str) or leaf_node.strip() == "" or any(err_keyword in leaf_node.lower() for err_keyword in ['error', 'exception', 'failed', 'invalid']):
            logger.debug(f"Skipping invalid leaf node identifier: '{leaf_node}'")
            continue

        if leaf_node in components:
            if components[leaf_node].component_type in valid_types:
                keep_leaf_nodes.append(leaf_node)

    return keep_leaf_nodes
