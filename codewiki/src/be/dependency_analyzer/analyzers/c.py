import logging
from typing import List, Optional, Tuple
from pathlib import Path
import sys
import os

from tree_sitter import Parser, Language
import tree_sitter_c
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship

logger = logging.getLogger(__name__)

class TreeSitterCAnalyzer:
	def __init__(self, file_path: str, content: str, repo_path: str = None):
		self.file_path = Path(file_path)
		self.content = content
		self.repo_path = repo_path or ""
		self.nodes: List[Node] = []
		self.call_relationships: List[CallRelationship] = []
		self._analyze()
	
	def _get_module_path(self) -> str:
		if self.repo_path:
			try:
				rel_path = os.path.relpath(str(self.file_path), self.repo_path)
			except ValueError:
				rel_path = str(self.file_path)
		else:
			rel_path = str(self.file_path)
		
		for ext in ['.c', '.h']:
			if rel_path.endswith(ext):
				rel_path = rel_path[:-len(ext)]
				break
		return rel_path.replace('/', '.').replace('\\', '.')
	
	def _get_relative_path(self) -> str:
		if self.repo_path:
			try:
				return os.path.relpath(str(self.file_path), self.repo_path)
			except ValueError:
				return str(self.file_path)
		else:
			return str(self.file_path)
	
	def _get_component_id(self, name: str) -> str:
		module_path = self._get_module_path()
		return f"{module_path}.{name}" if module_path else name

	def _analyze(self):
		language_capsule = tree_sitter_c.language()
		c_language = Language(language_capsule)
		parser = Parser(c_language)
		tree = parser.parse(bytes(self.content, "utf8"))
		root = tree.root_node
		lines = self.content.splitlines()
		
		top_level_nodes = {}
		
		# collect all top-level nodes using recursive traversal
		self._extract_nodes(root, top_level_nodes, lines)
		
		# extract relationships between top-level nodes
		self._extract_relationships(root, top_level_nodes)
	
	def _extract_nodes(self, node, top_level_nodes, lines):
		"""Recursively extract top-level nodes (functions and global variables)."""
		node_type = None
		node_name = None
		
		if node.type == "function_definition":
			node_type = "function"
			# look for function_declarator
			declarator = next((c for c in node.children if c.type == "function_declarator"), None)
			if declarator:
				identifier = next((c for c in declarator.children if c.type == "identifier"), None)
				if identifier:
					node_name = identifier.text.decode()
		elif node.type == "declaration":
			if self._is_global_variable(node):
				node_type = "variable"
				for child in node.children:
					if child.type == "init_declarator":
						identifier = next((c for c in child.children if c.type == "identifier"), None)
						if identifier:
							node_name = identifier.text.decode()
							break
						pointer_declarator = next((c for c in child.children if c.type == "pointer_declarator"), None)
						if pointer_declarator:
							identifier = next((c for c in pointer_declarator.children if c.type == "identifier"), None)
							if identifier:
								node_name = identifier.text.decode()
								break
					elif child.type == "identifier":
						node_name = child.text.decode()
						break
		
		if node_type and node_name:
			component_id = self._get_component_id(node_name)
			relative_path = self._get_relative_path()
			node_obj = Node(
				id=component_id,
				name=node_name,
				component_type=node_type,
				file_path=str(self.file_path),
				relative_path=relative_path,
				source_code="\n".join(lines[node.start_point[0]:node.end_point[0]+1]),
				start_line=node.start_point[0]+1,
				end_line=node.end_point[0]+1,
				has_docstring=False,
				docstring="",
				parameters=None,
				node_type=node_type,
				base_classes=None,
				class_name=None,
				display_name=f"{node_type} {node_name}",
				component_id=component_id
			)

			if node_type == "function":
				self.nodes.append(node_obj)
			top_level_nodes[node_name] = node_obj
		
		for child in node.children:
			self._extract_nodes(child, top_level_nodes, lines)
	
	def _is_global_variable(self, node) -> bool:
		parent = node.parent
		while parent:
			if parent.type == "function_definition":
				return False
			parent = parent.parent
		return True
	
	def _extract_relationships(self, node, top_level_nodes):
		"""Extract various types of relationships between top-level nodes."""
		
		# 1. function calls other functions
		if node.type == "call_expression":
			containing_function = self._find_containing_function(node, top_level_nodes)
			if containing_function:
				containing_function_id = self._get_component_id(containing_function)
				
				# Get called function name
				function_node = next((c for c in node.children if c.type == "identifier"), None)
				if function_node:
					called_function = function_node.text.decode()
					if not self._is_system_function(called_function):
						self.call_relationships.append(CallRelationship(
							caller=containing_function_id,
							callee=called_function,  # Use simple name for cross-file resolution
							call_line=node.start_point[0]+1,
							is_resolved=False  # Let CallGraphAnalyzer resolve
						))
		
		# 2. function uses global variables
		if node.type == "identifier":
			containing_function = self._find_containing_function(node, top_level_nodes)
			if containing_function:
				var_name = node.text.decode()
				# Check if this identifier refers to a global variable
				if var_name in top_level_nodes and top_level_nodes[var_name].component_type == "variable":
					containing_function_id = self._get_component_id(containing_function)
					var_component_id = self._get_component_id(var_name)
					self.call_relationships.append(CallRelationship(
						caller=containing_function_id,
						callee=var_component_id,
						call_line=node.start_point[0]+1,
						is_resolved=True  # Local file relationship
					))
		
		# Recursively process children
		for child in node.children:
			self._extract_relationships(child, top_level_nodes)
	
	def _find_containing_function(self, node, top_level_nodes):
		"""Find the function that contains this node."""
		current = node.parent
		while current:
			if current.type == "function_definition":
				# Get function name
				declarator = next((c for c in current.children if c.type == "function_declarator"), None)
				if declarator:
					identifier = next((c for c in declarator.children if c.type == "identifier"), None)
					if identifier:
						func_name = identifier.text.decode()
						if func_name in top_level_nodes:
							return func_name
			current = current.parent
		return None
	
	def _is_system_function(self, func_name: str) -> bool:
		"""Check if function is a system/library function."""
		# Common C library functions
		system_functions = {
			"printf", "scanf", "malloc", "free", "strlen", "strcpy", "strcmp", 
			"memcpy", "memset", "exit", "abort", "fopen", "fclose", "fread", "fwrite",
			"SDL_Init", "SDL_CreateWindow", "SDL_Log", "SDL_GetError", "SDL_Quit"
		}
		return func_name in system_functions

def analyze_c_file(file_path: str, content: str, repo_path: str = None) -> Tuple[List[Node], List[CallRelationship]]:
	analyzer = TreeSitterCAnalyzer(file_path, content, repo_path)
	return analyzer.nodes, analyzer.call_relationships
