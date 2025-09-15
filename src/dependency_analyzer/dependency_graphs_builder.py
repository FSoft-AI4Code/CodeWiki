from typing import Dict, List, Any
import os
from config import Config
from .ast_parser import DependencyParser
from .topo_sort import build_graph_from_components, get_leaf_nodes
from utils import file_manager

import logging
logger = logging.getLogger(__name__)


class DependencyGraphBuilder:
    """Handles dependency analysis and graph building."""
    
    def __init__(self, config: Config):
        self.config = config
    
    def build_dependency_graph(self) -> tuple[Dict[str, Any], List[str]]:
        """
        Build and save dependency graph, returning components and leaf nodes.
        
        Returns:
            Tuple of (components, leaf_nodes)
        """
        # Ensure output directory exists
        file_manager.ensure_directory(self.config.dependency_graph_dir)

        # Prepare dependency graph path
        repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
        sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
        dependency_graph_path = os.path.join(
            self.config.dependency_graph_dir, 
            f"{sanitized_repo_name}_dependency_graph.json"
        )
        filtered_folders_path = os.path.join(
            self.config.dependency_graph_dir, 
            f"{sanitized_repo_name}_filtered_folders.json"
        )

        parser = DependencyParser(self.config.repo_path)

        filtered_folders = None
        # if os.path.exists(filtered_folders_path):
        #     logger.info(f"Loading filtered folders from {filtered_folders_path}")
        #     filtered_folders = file_manager.load_json(filtered_folders_path)
        # else:
        #     # Parse repository
        #     filtered_folders = parser.filter_folders()
        #     # Save filtered folders
        #     file_manager.save_json(filtered_folders, filtered_folders_path)

        # Parse repository
        components = parser.parse_repository(filtered_folders)
        
        # Save dependency graph
        parser.save_dependency_graph(dependency_graph_path)
        
        # Build graph for traversal
        graph = build_graph_from_components(components)
        
        # Get leaf nodes
        leaf_nodes = get_leaf_nodes(graph, components)

        # check if leaf_nodes are in components, only keep the ones that are in components and type is one of the following: class, interface, struct
        keep_leaf_nodes = []
        for leaf_node in leaf_nodes:
            # Skip any leaf nodes that are clearly error strings or invalid identifiers
            if not isinstance(leaf_node, str) or leaf_node.strip() == "" or any(err_keyword in leaf_node.lower() for err_keyword in ['error', 'exception', 'failed', 'invalid']):
                logger.warning(f"Skipping invalid leaf node identifier: '{leaf_node}'")
                continue
                
            if leaf_node in components:
                if components[leaf_node].component_type in ["class", "interface", "struct"]:
                    keep_leaf_nodes.append(leaf_node)
                else:
                    logger.debug(f"Leaf node {leaf_node} is a {components[leaf_node].component_type}, removing it")
            else:
                logger.warning(f"Leaf node {leaf_node} not found in components, removing it")
        
        return components, keep_leaf_nodes