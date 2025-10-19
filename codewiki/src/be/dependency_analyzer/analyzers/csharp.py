import logging
from typing import List, Optional, Tuple
from pathlib import Path
import sys
import os

from tree_sitter import Parser, Language
import tree_sitter_c_sharp
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship

logger = logging.getLogger(__name__)

class TreeSitterCSharpAnalyzer:
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
		
		for ext in ['.cs']:
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
		language_capsule = tree_sitter_c_sharp.language()
		cs_language = Language(language_capsule)
		parser = Parser(cs_language)
		tree = parser.parse(bytes(self.content, "utf8"))
		root = tree.root_node
		lines = self.content.splitlines()
		
		top_level_nodes = {}
	
		self._extract_nodes(root, top_level_nodes, lines)
		
		self._extract_relationships(root, top_level_nodes)
	
	def _extract_nodes(self, node, top_level_nodes, lines):
		node_type = None
		node_name = None
		
		if node.type == "class_declaration":
			# modifiers + class + identifier + body
			is_abstract = any(c.type == "modifier" and "abstract" in c.text.decode() for c in node.children)
			is_static = any(c.type == "modifier" and "static" in c.text.decode() for c in node.children)
			if is_static:
				node_type = "static class"
			elif is_abstract:
				node_type = "abstract class"
			else:
				node_type = "class"
			# find identifier that comes after class keyword
			found_class_keyword = False
			for child in node.children:
				if child.type == "class":
					found_class_keyword = True
				elif found_class_keyword and child.type == "identifier":
					node_name = child.text.decode()
					break
		elif node.type == "interface_declaration":
			node_type = "interface"
			# find identifier that comes after interface keyword
			found_interface_keyword = False
			for child in node.children:
				if child.type == "interface":
					found_interface_keyword = True
				elif found_interface_keyword and child.type == "identifier":
					node_name = child.text.decode()
					break
		elif node.type == "struct_declaration":
			node_type = "struct"
			# find identifier that comes after struct keyword
			found_struct_keyword = False
			for child in node.children:
				if child.type == "struct":
					found_struct_keyword = True
				elif found_struct_keyword and child.type == "identifier":
					node_name = child.text.decode()
					break
		elif node.type == "enum_declaration":
			node_type = "enum"
			# find identifier that comes after enum keyword
			found_enum_keyword = False
			for child in node.children:
				if child.type == "enum":
					found_enum_keyword = True
				elif found_enum_keyword and child.type == "identifier":
					node_name = child.text.decode()
					break
		elif node.type == "record_declaration":
			node_type = "record"
			# find identifier that comes after record keyword
			found_record_keyword = False
			for child in node.children:
				if child.type == "record":
					found_record_keyword = True
				elif found_record_keyword and child.type == "identifier":
					node_name = child.text.decode()
					break
		elif node.type == "delegate_declaration":
			node_type = "delegate"
			for child in node.children:
				if child.type == "identifier":
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
			self.nodes.append(node_obj)
			top_level_nodes[node_name] = node_obj
		
		for child in node.children:
			self._extract_nodes(child, top_level_nodes, lines)
	
	def _extract_relationships(self, node, top_level_nodes):
		containing_class = self._find_containing_class(node, top_level_nodes)
		
		if node.type == "class_declaration":
			class_name = self._get_identifier_name_cs(node)
			if class_name:
				class_component_id = self._get_component_id(class_name)
				
				base_list = next((c for c in node.children if c.type == "base_list"), None)
				if base_list:
					for child in base_list.children:
						if child.type == "identifier":
							base_name = child.text.decode()
							if base_name in [n.name for n in top_level_nodes.values()]:
								base_component_id = self._get_component_id(base_name)
								self.call_relationships.append(CallRelationship(
									caller=class_component_id,
									callee=base_component_id,
									call_line=node.start_point[0]+1,
									is_resolved=True
								))
		
		elif node.type == "property_declaration":
			if containing_class:
				containing_class_id = self._get_component_id(containing_class)
				type_identifiers = [c for c in node.children if c.type == "identifier"]
				if len(type_identifiers) >= 2:
					property_type = type_identifiers[0].text.decode()
					if property_type and not self._is_primitive_type(property_type):
						self.call_relationships.append(CallRelationship(
							caller=containing_class_id,
							callee=property_type,  
							call_line=node.start_point[0]+1,
							is_resolved=False  
						))
		
		elif node.type == "field_declaration":
			if containing_class:
				containing_class_id = self._get_component_id(containing_class)
				type_node = next((c for c in node.children if c.type == "identifier"), None)
				if type_node:
					field_type = type_node.text.decode()
					if field_type and not self._is_primitive_type(field_type):
						self.call_relationships.append(CallRelationship(
							caller=containing_class_id,
							callee=field_type, 
							call_line=node.start_point[0]+1,
							is_resolved=False  
						))
		
		elif node.type == "method_declaration":
			if containing_class:
				containing_class_id = self._get_component_id(containing_class)
				param_list = next((c for c in node.children if c.type == "parameter_list"), None)
				if param_list:
					for child in param_list.children:
						if child.type == "parameter":
							type_node = next((c for c in child.children if c.type == "identifier"), None)
							if type_node:
								param_type = type_node.text.decode()
								if param_type and not self._is_primitive_type(param_type):
									self.call_relationships.append(CallRelationship(
										caller=containing_class_id,
										callee=param_type,  
										call_line=node.start_point[0]+1,
										is_resolved=False  
									))
		
		# Recursively process children
		for child in node.children:
			self._extract_relationships(child, top_level_nodes)
	
	def _is_primitive_type(self, type_name: str) -> bool:
		"""Check if type is a C# primitive or common built-in type."""
		primitives = {
			"bool", "byte", "sbyte", "char", "decimal", "double", "float", "int", "uint", 
			"long", "ulong", "short", "ushort", "string", "object", "void",
			"Boolean", "Byte", "SByte", "Char", "Decimal", "Double", "Single", "Int32", "UInt32",
			"Int64", "UInt64", "Int16", "UInt16", "String", "Object", "Void",
			"List", "Dictionary", "IList", "IDictionary", "IEnumerable", "ICollection",
			"Task", "CancellationToken", "DateTime", "TimeSpan", "Guid"
		}
		return type_name in primitives
	
	def _get_identifier_name(self, node):
		name_node = next((c for c in node.children if c.type == "identifier"), None)
		return name_node.text.decode() if name_node else None
	
	def _get_identifier_name_cs(self, node):
		if node.type == "class_declaration":
			found_class_keyword = False
			for child in node.children:
				if child.type == "class":
					found_class_keyword = True
				elif found_class_keyword and child.type == "identifier":
					return child.text.decode()
		elif node.type == "interface_declaration":
			found_interface_keyword = False
			for child in node.children:
				if child.type == "interface":
					found_interface_keyword = True
				elif found_interface_keyword and child.type == "identifier":
					return child.text.decode()
		elif node.type == "struct_declaration":
			found_struct_keyword = False
			for child in node.children:
				if child.type == "struct":
					found_struct_keyword = True
				elif found_struct_keyword and child.type == "identifier":
					return child.text.decode()
		name_node = next((c for c in node.children if c.type == "identifier"), None)
		return name_node.text.decode() if name_node else None
	
	def _get_type_name(self, node):
		"""Get type name from a type node."""
		if node.type == "identifier":
			return node.text.decode()
		elif node.type == "generic_name":
			type_node = next((c for c in node.children if c.type == "identifier"), None)
			return type_node.text.decode() if type_node else None
		elif node.type == "predefined_type":
			return node.text.decode()
		return None
	
	def _find_containing_class(self, node, top_level_nodes):
		current = node.parent
		while current:
			if current.type in ["class_declaration", "interface_declaration", "struct_declaration", "enum_declaration", "record_declaration", "delegate_declaration"]:
				class_name = self._get_identifier_name_cs(current)
				if class_name and class_name in top_level_nodes:
					return class_name
			current = current.parent
		return None
	
def analyze_csharp_file(file_path: str, content: str, repo_path: str = None) -> Tuple[List[Node], List[CallRelationship]]:
	analyzer = TreeSitterCSharpAnalyzer(file_path, content, repo_path)
	return analyzer.nodes, analyzer.call_relationships

