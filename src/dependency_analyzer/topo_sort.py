# Copyright (c) Meta Platforms, Inc. and affiliates
"""
Topological sorting utilities for dependency graphs with cycle handling.

This module provides functions to perform topological sorting on a dependency graph,
including detection and resolution of dependency cycles.
"""

import logging
from typing import Dict, List, Set, Tuple, Any, Optional
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


def detect_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """
    Detect cycles in a dependency graph using Tarjan's algorithm to find
    strongly connected components.
    
    Args:
        graph: A dependency graph represented as adjacency lists
               (node -> set of dependencies)
    
    Returns:
        A list of lists, where each inner list contains the nodes in a cycle
    """
    # Implementation of Tarjan's algorithm
    index_counter = [0]
    index = {}  # node -> index
    lowlink = {}  # node -> lowlink value
    onstack = set()  # nodes currently on the stack
    stack = []  # stack of nodes
    result = []  # list of cycles (strongly connected components)
    
    def strongconnect(node):
        # Set the depth index for node
        index[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        onstack.add(node)
        
        # Consider successors
        for successor in graph.get(node, set()):
            if successor not in index:
                # Successor has not yet been visited; recurse on it
                strongconnect(successor)
                lowlink[node] = min(lowlink[node], lowlink[successor])
            elif successor in onstack:
                # Successor is on the stack and hence in the current SCC
                lowlink[node] = min(lowlink[node], index[successor])
        
        # If node is a root node, pop the stack and generate an SCC
        if lowlink[node] == index[node]:
            # Start a new strongly connected component
            scc = []
            while True:
                successor = stack.pop()
                onstack.remove(successor)
                scc.append(successor)
                if successor == node:
                    break
            
            # Only include SCCs with more than one node (actual cycles)
            if len(scc) > 1:
                result.append(scc)
    
    # Visit each node
    for node in graph:
        if node not in index:
            strongconnect(node)
    
    return result

def resolve_cycles(graph: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """
    Resolve cycles in a dependency graph by identifying strongly connected
    components and breaking cycles.
    
    Args:
        graph: A dependency graph represented as adjacency lists
               (node -> set of dependencies)
    
    Returns:
        A new acyclic graph with the same nodes but with cycles broken
    """
    # Detect cycles (SCCs)
    cycles = detect_cycles(graph)
    
    if not cycles:
        logger.info("No cycles detected in the dependency graph")
        return graph
    
    logger.info(f"Detected {len(cycles)} cycles in the dependency graph")
    
    # Create a copy of the graph to modify
    new_graph = {node: deps.copy() for node, deps in graph.items()}
    
    # Process each cycle
    for i, cycle in enumerate(cycles):
        logger.info(f"Cycle {i+1}: {' -> '.join(cycle)}")
        
        # Strategy: Break the cycle by removing the "weakest" dependency
        # Here, we just arbitrarily remove the last edge to make the graph acyclic
        # In a real-world scenario, you might use heuristics to determine which edge to break
        # For example, removing edges between different modules before edges within the same module
        for j in range(len(cycle) - 1):
            current = cycle[j]
            next_node = cycle[j + 1]
            
            if next_node in new_graph[current]:
                logger.info(f"Breaking cycle by removing dependency: {current} -> {next_node}")
                new_graph[current].remove(next_node)
                break
    
    return new_graph

def topological_sort(graph: Dict[str, Set[str]]) -> List[str]:
    """
    Perform a topological sort on a dependency graph.
    
    Args:
        graph: A dependency graph represented as adjacency lists
               (node -> set of dependencies)
    
    Returns:
        A list of nodes in topological order (dependencies first)
    """
    # First, check for and resolve cycles
    acyclic_graph = resolve_cycles(graph)
    
    # Initialize in-degree counter for all nodes
    in_degree = {node: 0 for node in acyclic_graph}
    
    # Count in-degrees
    for node, dependencies in acyclic_graph.items():
        for dep in dependencies:
            if dep in in_degree:
                in_degree[dep] += 1
    
    # Queue of nodes with no dependencies (in-degree of 0)
    queue = deque([node for node, degree in in_degree.items() if degree == 0])
    
    # Result list to store the topological order
    result = []
    
    # Process nodes in topological order
    while queue:
        node = queue.popleft()
        result.append(node)
        
        # Reduce in-degree for each node that depends on the current node
        for dependent, deps in acyclic_graph.items():
            if node in deps:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
    
    # Check if the sort was successful (all nodes included)
    if len(result) != len(acyclic_graph):
        logger.warning("Topological sort failed: graph has cycles that weren't resolved")
        # Return all nodes in some order to avoid breaking the process
        return list(acyclic_graph.keys())
    
    # Reverse the result to get dependencies first
    return result[::-1]

def dependency_first_dfs(graph: Dict[str, Set[str]]) -> List[str]:
    """
    Perform a depth-first traversal of the dependency graph, starting from root nodes
    that have no dependencies.
    
    The graph uses natural dependency direction:
    - If A depends on B, the graph has an edge A → B
    - This means an edge from X to Y represents "X depends on Y"
    - Root nodes (nodes with no incoming edges/dependencies) are processed first,
      followed by nodes that depend on them
    
    Args:
        graph: A dependency graph with natural direction (A→B if A depends on B)
    
    Returns:
        A list of nodes in an order where dependencies come before their dependents
    """
    # First, resolve cycles to ensure we have a DAG
    acyclic_graph = resolve_cycles(graph)
    
    # Find root nodes (nodes with no dependencies)
    root_nodes = []
    # Create a reverse graph to easily check if a node has incoming edges
    has_incoming_edge = {node: False for node in acyclic_graph}
    
    for node, deps in acyclic_graph.items():
        for dep in deps:
            has_incoming_edge[dep] = True
    
    # Nodes with no incoming edges are root nodes
    for node in acyclic_graph:
        if not has_incoming_edge.get(node, False) and node in acyclic_graph:
            root_nodes.append(node)
    
    if not root_nodes:
        logger.warning("No root nodes found in the graph, using arbitrary starting point")
        root_nodes = list(acyclic_graph.keys())[:1]  # Use the first node as starting point
    
    # Track visited nodes
    visited = set()
    result = []
    
    # DFS function that processes dependencies first
    def dfs(node):
        if node in visited:
            return
        visited.add(node)
        
        # Visit all dependencies first
        for dep in sorted(acyclic_graph.get(node, set())):
            dfs(dep)
        
        # Add this node to the result after all its dependencies
        result.append(node)
    
    # Start DFS from each root node
    for root in sorted(root_nodes):
        dfs(root)
    
    # Check if all nodes were visited
    if len(result) != len(acyclic_graph):
        # Some nodes weren't visited - try to visit remaining nodes
        for node in sorted(acyclic_graph.keys()):
            if node not in visited:
                dfs(node)
    
    return result

def build_graph_from_components(components: Dict[str, Any]) -> Dict[str, Set[str]]:
    """
    Build a dependency graph from a collection of code components.
    
    The graph uses the natural dependency direction:
    - If A depends on B, we create an edge A → B
    - This means an edge from node X to node Y represents "X depends on Y"
    - Root nodes (nodes with no dependencies) are components that don't depend on anything
    
    Args:
        components: A dictionary of code components, where each component
                   has a 'depends_on' attribute
    
    Returns:
        A dependency graph with natural dependency direction
    """
    graph = {}
    
    for comp_id, component in components.items():
        # Initialize the node's adjacency list
        if comp_id not in graph:
            graph[comp_id] = set()
        
        # Add dependencies
        for dep_id in component.depends_on:
            # Only include dependencies that are actual components in our repository
            if dep_id in components:
                graph[comp_id].add(dep_id)
    
    return graph 


def get_leaf_nodes(graph: Dict[str, Set[str]]) -> List[str]:
    """
    Find leaf nodes (nodes that no other nodes depend on) and build dependency trees
    showing the full dependency chain from each leaf back to the ultimate dependencies.
    
    The graph uses natural dependency direction:
    - If A depends on B, the graph has an edge A → B
    - Leaf nodes are nodes that appear in no other node's dependency set
    - Each tree shows the dependency chain: leaf → its dependencies → their dependencies, etc.
    
    Args:
        graph: A dependency graph with natural direction (A→B if A depends on B)
    
    Returns:
        A list of leaf nodes
    """
    # First, resolve cycles to ensure we have a DAG
    acyclic_graph = resolve_cycles(graph)
    
    # Find leaf nodes (nodes that no other nodes depend on)
    leaf_nodes = set(acyclic_graph.keys())
    
    # Remove nodes that are dependencies of other nodes
    for node, deps in acyclic_graph.items():
        for dep in deps:
            leaf_nodes.discard(dep)
    
    concise_leaf_nodes = set()
    for node in leaf_nodes:
        if node.endswith("__init__"):
            # replace by class name
            concise_leaf_nodes.add(node.replace(".__init__", ""))
        else:
            concise_leaf_nodes.add(node)
    
    if not leaf_nodes:
        logger.warning("No leaf nodes found in the graph")
        return []
    
    return concise_leaf_nodes 