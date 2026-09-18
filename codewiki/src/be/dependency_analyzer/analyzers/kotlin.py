import logging
from typing import List, Optional, Tuple
from pathlib import Path
import os

from tree_sitter import Parser, Language
import tree_sitter_kotlin
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship

logger = logging.getLogger(__name__)


class TreeSitterKotlinAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: Optional[str] = None):
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

        for ext in [".kt", ".kts"]:
            if rel_path.endswith(ext):
                rel_path = rel_path[: -len(ext)]
                break
        return rel_path.replace("/", ".").replace("\\", ".")

    def _get_relative_path(self) -> str:
        """Get relative path from repo root."""
        if self.repo_path:
            try:
                return os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                return str(self.file_path)
        else:
            return str(self.file_path)

    def _get_component_id(self, name: str, parent_class: Optional[str] = None) -> str:
        rel_path = self._get_relative_path()
        if parent_class:
            return f"{rel_path}::{parent_class}.{name}"
        else:
            return f"{rel_path}::{name}"

    def _analyze(self):
        try:
            language_capsule = tree_sitter_kotlin.language()
            kotlin_language = Language(language_capsule)
            parser = Parser(kotlin_language)
            tree = parser.parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            top_level_nodes = {}

            self._extract_nodes(root, top_level_nodes, lines)
            self._extract_relationships(root, top_level_nodes)
        except Exception as e:
            logger.error(f"Error parsing Kotlin file {self.file_path}: {e}")

    def _extract_nodes(self, node, top_level_nodes, lines):
        node_type = None
        node_name = None

        if node.type == "class_declaration":
            is_interface = any(c.type == "interface" for c in node.children)

            if is_interface:
                node_type = "interface"
            else:
                modifiers = self._get_class_modifiers(node)
                if "abstract" in modifiers:
                    node_type = "abstract class"
                elif "data" in modifiers:
                    node_type = "data class"
                elif "enum" in modifiers:
                    node_type = "enum class"
                elif "annotation" in modifiers:
                    node_type = "annotation class"
                else:
                    node_type = "class"

            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "object_declaration":
            node_type = "object"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "function_declaration":
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node:
                method_name = name_node.text.decode()
                containing_class = self._find_containing_class_name(node)
                if containing_class:
                    node_type = "method"
                    node_name = f"{containing_class}.{method_name}"
                else:
                    node_type = "function"
                    node_name = method_name

        if node_type and node_name:
            component_id = self._get_component_id(node_name)
            relative_path = self._get_relative_path()

            # Extract docstring if present
            docstring = ""
            if node.prev_sibling and hasattr(node.prev_sibling, "type"):
                if node.prev_sibling.type in ("line_comment", "block_comment"):
                    docstring = node.prev_sibling.text.decode().strip()

            # Safely extract code lines
            start_line_idx = node.start_point[0]
            end_line_idx = node.end_point[0] + 1
            code_snippet = (
                "\n".join(lines[start_line_idx:end_line_idx]) if start_line_idx < len(lines) else ""
            )

            node_obj = Node(
                id=component_id,
                name=node_name,
                component_type=node_type,
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code=code_snippet,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                has_docstring=bool(docstring),
                docstring=docstring,
                parameters=None,
                node_type=node_type,
                base_classes=None,
                class_name=None,
                display_name=f"{node_type} {node_name}",
                component_id=component_id,
            )
            self.nodes.append(node_obj)
            top_level_nodes[node_name] = node_obj

        for child in node.children:
            self._extract_nodes(child, top_level_nodes, lines)

    def _get_class_modifiers(self, class_node) -> set:
        """Extract class modifiers (abstract, data, enum, annotation, etc.)."""
        modifiers = set()
        modifiers_node = next((c for c in class_node.children if c.type == "modifiers"), None)
        if modifiers_node:
            for mod in modifiers_node.children:
                if mod.type in ("class_modifier", "inheritance_modifier", "visibility_modifier"):
                    for inner in mod.children:
                        modifiers.add(inner.type)
        return modifiers

    def _extract_relationships(self, node, top_level_nodes):
        # 1. Inheritance and Interface Implementation via delegation_specifiers
        if node.type == "class_declaration":
            class_name = self._get_identifier_name(node)
            delegation_specifiers = next(
                (c for c in node.children if c.type == "delegation_specifiers"), None
            )
            if delegation_specifiers and class_name:
                for spec in delegation_specifiers.children:
                    if spec.type == "delegation_specifier":
                        for child in spec.children:
                            type_name = None
                            if child.type == "constructor_invocation":
                                user_type = next(
                                    (c for c in child.children if c.type == "user_type"), None
                                )
                                if user_type:
                                    type_name = self._get_type_name(user_type)
                            elif child.type == "user_type":
                                type_name = self._get_type_name(child)

                            if type_name and not self._is_primitive_type(type_name):
                                caller_id = self._get_component_id(class_name)
                                callee_id = self._get_component_id(type_name)
                                self.call_relationships.append(
                                    CallRelationship(
                                        caller=caller_id,
                                        callee=callee_id,
                                        call_line=node.start_point[0] + 1,
                                        is_resolved=False,
                                    )
                                )

        # 2. Property Type Use (field types)
        if node.type == "property_declaration":
            containing_class = self._find_containing_class(node, top_level_nodes)
            var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
            if containing_class and var_decl:
                type_node = next((c for c in var_decl.children if c.type == "user_type"), None)
                if type_node:
                    prop_type_name = self._get_type_name(type_node)
                    if prop_type_name and not self._is_primitive_type(prop_type_name):
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_class,
                                callee=prop_type_name,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

        # 3. Constructor parameter type use
        if node.type == "class_parameter":
            containing_class_node = node.parent
            while containing_class_node and containing_class_node.type != "class_declaration":
                containing_class_node = containing_class_node.parent
            if containing_class_node:
                class_name = self._get_identifier_name(containing_class_node)
                if class_name and class_name in top_level_nodes:
                    type_node = next((c for c in node.children if c.type == "user_type"), None)
                    if type_node:
                        param_type = self._get_type_name(type_node)
                        if param_type and not self._is_primitive_type(param_type):
                            caller_id = self._get_component_id(class_name)
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=caller_id,
                                    callee=param_type,
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )

        # 4. Method Calls / Function invocations
        if node.type == "call_expression":
            caller_id = self._find_containing_method(node) or self._find_containing_class(
                node, top_level_nodes
            )

            target_expr = next(
                (c for c in node.children if c.type in ["identifier", "navigation_expression"]),
                None,
            )

            if target_expr and caller_id:
                if target_expr.type == "identifier":
                    callee_name = target_expr.text.decode()
                    if (
                        callee_name
                        and callee_name[0].isupper()
                        and not self._is_primitive_type(callee_name)
                    ):
                        callee_id = self._get_component_id(callee_name)
                        self.call_relationships.append(
                            CallRelationship(
                                caller=caller_id,
                                callee=callee_id,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )
                    elif callee_name and not self._is_primitive_type(callee_name):
                        self.call_relationships.append(
                            CallRelationship(
                                caller=caller_id,
                                callee=callee_name,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

                elif target_expr.type == "navigation_expression":
                    children = list(target_expr.children)
                    object_node = next((c for c in children if c.type == "identifier"), None)
                    method_node = None
                    identifiers = [c for c in children if c.type == "identifier"]
                    if len(identifiers) >= 2:
                        object_node = identifiers[0]
                        method_node = identifiers[-1]
                    elif len(identifiers) == 1:
                        method_node = identifiers[0]
                        nav_child = next(
                            (c for c in children if c.type == "navigation_expression"), None
                        )
                        if nav_child:
                            object_node = self._get_root_identifier(nav_child)
                        else:
                            object_node = None

                    if object_node and method_node:
                        object_name = (
                            object_node.text.decode()
                            if hasattr(object_node, "text")
                            else str(object_node)
                        )

                        target_type = None
                        if object_name in top_level_nodes:
                            target_type = object_name
                        else:
                            target_type = self._find_variable_type(
                                node, object_name, top_level_nodes
                            )

                        if target_type and not self._is_primitive_type(target_type):
                            callee_id = self._get_component_id(target_type)
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=caller_id,
                                    callee=callee_id,
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )
                    elif method_node and not object_node:
                        callee_name = method_node.text.decode()
                        self.call_relationships.append(
                            CallRelationship(
                                caller=caller_id,
                                callee=callee_name,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

        for child in node.children:
            self._extract_relationships(child, top_level_nodes)

    def _is_primitive_type(self, type_name: str) -> bool:
        """Check if type is a Kotlin primitive or common built-in type."""
        primitives = {
            "Boolean",
            "Byte",
            "Char",
            "Double",
            "Float",
            "Int",
            "Long",
            "Short",
            "String",
            "Unit",
            "Nothing",
            "Any",
            "List",
            "Set",
            "Map",
            "Collection",
            "Iterable",
            "Sequence",
            "MutableList",
            "MutableSet",
            "MutableMap",
            "MutableCollection",
            "Array",
            "IntArray",
            "LongArray",
            "FloatArray",
            "DoubleArray",
            "BooleanArray",
            "ByteArray",
            "CharArray",
            "ShortArray",
            "Pair",
            "Triple",
        }
        return type_name in primitives

    def _get_identifier_name(self, node):
        """Get identifier name from a node."""
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        return name_node.text.decode() if name_node else None

    def _get_type_name(self, node) -> Optional[str]:
        """Get the primary type name from a type node, stripping generics."""
        if node.type == "user_type":
            id_node = next((c for c in node.children if c.type == "identifier"), None)
            return id_node.text.decode() if id_node else None
        elif node.type == "nullable_type":
            inner = next((c for c in node.children if c.type == "user_type"), None)
            if inner:
                return self._get_type_name(inner)
        elif node.type == "identifier":
            return node.text.decode()
        return None

    def _get_root_identifier(self, nav_node):
        """Get the root identifier from a chain of navigation_expressions."""
        first_child = nav_node.children[0] if nav_node.children else None
        if first_child:
            if first_child.type == "identifier":
                return first_child
            elif first_child.type == "navigation_expression":
                return self._get_root_identifier(first_child)
        return None

    def _find_containing_class_name(self, node):
        """Walk up to find the containing class/object/interface name."""
        current = node.parent
        while current:
            if current.type in ("class_declaration", "object_declaration"):
                name_node = next((c for c in current.children if c.type == "identifier"), None)
                if name_node:
                    return name_node.text.decode()
            current = current.parent
        return None

    def _find_containing_class(self, node, top_level_nodes):
        """Find the component ID of the containing class."""
        class_name = self._find_containing_class_name(node)
        if class_name and class_name in top_level_nodes:
            return self._get_component_id(class_name)
        return None

    def _find_containing_method(self, node):
        """Find the component ID of the containing function/method."""
        current = node.parent
        while current:
            if current.type == "function_declaration":
                method_name = self._get_identifier_name(current)
                class_name = self._find_containing_class_name(current)
                if method_name:
                    if class_name:
                        return self._get_component_id(f"{class_name}.{method_name}")
                    return self._get_component_id(method_name)
            current = current.parent
        return None

    def _find_variable_type(self, node, variable_name: str, top_level_nodes) -> Optional[str]:
        """
        Try to resolve the type of a variable by searching local declarations,
        function parameters, constructor parameters, and class properties.
        """
        func_node = node.parent
        while func_node and func_node.type != "function_declaration":
            func_node = func_node.parent

        if func_node:
            params_node = next(
                (c for c in func_node.children if c.type == "function_value_parameters"), None
            )
            if params_node:
                for param in params_node.children:
                    if param.type == "parameter":
                        param_name_node = next(
                            (c for c in param.children if c.type == "identifier"), None
                        )
                        if param_name_node and param_name_node.text.decode() == variable_name:
                            type_node = next(
                                (
                                    c
                                    for c in param.children
                                    if c.type in ("user_type", "nullable_type")
                                ),
                                None,
                            )
                            if type_node:
                                return self._get_type_name(type_node)

            body_node = next((c for c in func_node.children if c.type == "function_body"), None)
            if body_node:
                block = next((c for c in body_node.children if c.type == "block"), None)
                if block:
                    result = self._search_variable_declaration(block, variable_name)
                    if result:
                        return result

        class_node = node.parent
        while class_node and class_node.type not in ("class_declaration", "object_declaration"):
            class_node = class_node.parent

        if class_node:
            primary_ctor = next(
                (c for c in class_node.children if c.type == "primary_constructor"), None
            )
            if primary_ctor:
                class_params = next(
                    (c for c in primary_ctor.children if c.type == "class_parameters"), None
                )
                if class_params:
                    for param in class_params.children:
                        if param.type == "class_parameter":
                            param_name = next(
                                (c for c in param.children if c.type == "identifier"), None
                            )
                            if param_name and param_name.text.decode() == variable_name:
                                type_node = next(
                                    (
                                        c
                                        for c in param.children
                                        if c.type in ("user_type", "nullable_type")
                                    ),
                                    None,
                                )
                                if type_node:
                                    return self._get_type_name(type_node)

            class_body = next(
                (c for c in class_node.children if c.type in ("class_body", "enum_class_body")),
                None,
            )
            if class_body:
                for body_child in class_body.children:
                    if body_child.type == "property_declaration":
                        var_decl = next(
                            (c for c in body_child.children if c.type == "variable_declaration"),
                            None,
                        )
                        if var_decl:
                            prop_name = next(
                                (c for c in var_decl.children if c.type == "identifier"), None
                            )
                            if prop_name and prop_name.text.decode() == variable_name:
                                type_node = next(
                                    (
                                        c
                                        for c in var_decl.children
                                        if c.type in ("user_type", "nullable_type")
                                    ),
                                    None,
                                )
                                if type_node:
                                    return self._get_type_name(type_node)

        return None

    def _search_variable_declaration(self, block_node, variable_name: str) -> Optional[str]:
        """Search for a local variable declaration with explicit type annotation in a block."""
        for child in block_node.children:
            if child.type == "property_declaration":
                var_decl = next(
                    (c for c in child.children if c.type == "variable_declaration"), None
                )
                if var_decl:
                    name_node = next((c for c in var_decl.children if c.type == "identifier"), None)
                    if name_node and name_node.text.decode() == variable_name:
                        type_node = next(
                            (
                                c
                                for c in var_decl.children
                                if c.type in ("user_type", "nullable_type")
                            ),
                            None,
                        )
                        if type_node:
                            return self._get_type_name(type_node)

                        init_expr = next(
                            (c for c in child.children if c.type == "call_expression"), None
                        )
                        if init_expr:
                            call_id = next(
                                (c for c in init_expr.children if c.type == "identifier"), None
                            )
                            if call_id:
                                inferred = call_id.text.decode()
                                if inferred and inferred[0].isupper():
                                    return inferred

            elif child.type == "block":
                result = self._search_variable_declaration(child, variable_name)
                if result:
                    return result

        return None


def analyze_kotlin_file(
    file_path: str, content: str, repo_path: Optional[str] = None
) -> Tuple[List[Node], List[CallRelationship]]:
    analyzer = TreeSitterKotlinAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
