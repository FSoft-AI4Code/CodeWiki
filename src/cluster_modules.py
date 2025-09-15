from typing import List, Dict, Any
from collections import defaultdict

from dependency_analyzer.models.core import Node
from llm_services import call_llm
from utils import count_tokens
from config import MAX_TOKEN_PER_MODULE, MAIN_MODEL
from prompt_template import format_cluster_prompt


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
            import logging
            logger = logging.getLogger(__name__)
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


def cluster_modules(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    current_module_tree: dict[str, Any] = {},
    current_module_name: str = None,
    current_module_path: List[str] = []
) -> Dict[str, Any]:
    """
    Cluster the potential core components into modules.
    """
    potential_core_components, potential_core_components_with_code = format_potential_core_components(leaf_nodes, components)

    if count_tokens(potential_core_components_with_code) <= MAX_TOKEN_PER_MODULE:
        return {}

    prompt = format_cluster_prompt(potential_core_components, current_module_tree, current_module_name)
    response = call_llm(prompt, model=MAIN_MODEL)

    #parse the response
    try:
        if "<GROUPED_COMPONENTS>" not in response or "</GROUPED_COMPONENTS>" not in response:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Invalid LLM response format - missing component tags: {response[:200]}...")
            return {}
        
        response_content = response.split("<GROUPED_COMPONENTS>")[1].split("</GROUPED_COMPONENTS>")[0]
        module_tree = eval(response_content)
        
        if not isinstance(module_tree, dict):
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Invalid module tree format - expected dict, got {type(module_tree)}")
            return {}
            
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to parse LLM response: {e}. Response: {response[:200]}...")
        return {}

    # check if the module tree is valid
    if len(module_tree) <= 1:
        return {}

    if current_module_tree == {}:
        current_module_tree = module_tree
    else:
        value = current_module_tree
        for key in current_module_path:
            value = value[key]["children"]
        for module_name, module_info in module_tree.items():
            del module_info["path"]
            value[module_name] = module_info

    for module_name, module_info in module_tree.items():
        sub_leaf_nodes = module_info.get("components", [])
        
        # Filter sub_leaf_nodes to ensure they exist in components
        valid_sub_leaf_nodes = []
        for node in sub_leaf_nodes:
            if node in components:
                valid_sub_leaf_nodes.append(node)
            else:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Skipping invalid sub leaf node '{node}' in module '{module_name}' - not found in components")
        
        current_module_path.append(module_name)
        module_info["children"] = {}
        module_info["children"] = cluster_modules(valid_sub_leaf_nodes, components, current_module_tree, module_name, current_module_path)
        current_module_path.pop()

    return module_tree