import logging
import os
import traceback
from typing import List, Set, Optional, Tuple
from pathlib import Path

from tree_sitter import Parser, Language
import tree_sitter_typescript

from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    JS_TS_PROTOTYPE_METHODS,
)

logger = logging.getLogger(__name__)


class TreeSitterTSAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []
        self.seen_relationships: Set[Tuple[str, str]] = set()

        self.top_level_nodes = {}

        try:
            language_capsule = tree_sitter_typescript.language_typescript()
            self.ts_language = Language(language_capsule)
            self.parser = Parser(self.ts_language)

        except Exception as e:
            logger.error(f"Failed to initialize TypeScript parser: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            self.parser = None
            self.ts_language = None

    def analyze(self) -> None:
        if self.parser is None:
            logger.debug(f"Skipping {self.file_path} - parser initialization failed")
            return

        try:
            tree = self.parser.parse(bytes(self.content, "utf8"))
            root_node = tree.root_node

            logger.debug(f"Parsed AST with root node type: {root_node.type}")

            all_entities = {}
            self._extract_all_entities(root_node, all_entities)

            self._filter_top_level_declarations(all_entities)

            self._extract_all_relationships(root_node, all_entities)

        except Exception as e:
            logger.error(f"Error analyzing TypeScript file {self.file_path}: {e}", exc_info=True)

    def _extract_all_entities(self, node, all_entities: dict, depth=0) -> None:
        entity = None
        entity_name = None

        if node.type == "function_declaration":
            entity = self._extract_function_entity(node, "function", depth)
        elif node.type == "generator_function_declaration":
            entity = self._extract_function_entity(node, "generator_function", depth)
        elif node.type == "arrow_function":
            entity = self._extract_arrow_function_entity(node, depth)
        elif node.type == "method_definition":
            entity = self._extract_method_entity(node, depth)
        elif node.type == "class_declaration":
            entity = self._extract_class_entity(node, "class", depth)
        elif node.type == "abstract_class_declaration":
            entity = self._extract_class_entity(node, "abstract_class", depth)
        elif node.type == "interface_declaration":
            entity = self._extract_interface_entity(node, depth)
        elif node.type == "type_alias_declaration":
            entity = self._extract_type_alias_entity(node, depth)
        elif node.type == "enum_declaration":
            entity = self._extract_enum_entity(node, depth)
        elif node.type == "variable_declarator":
            entity = self._extract_variable_entity(node, depth)
        elif node.type == "export_statement":
            entity = self._extract_export_statement_entity(node, depth)
        elif node.type == "lexical_declaration":
            entity = self._extract_lexical_declaration_entity(node, depth)
        elif node.type == "variable_declaration":
            entity = self._extract_variable_declaration_entity(node, depth)
        elif node.type == "ambient_declaration":
            entity = self._extract_ambient_declaration_entity(node, depth)

        if entity and entity.get("name"):
            entity_name = entity["name"]
            entity["depth"] = depth
            entity["node"] = node
            entity["parent_context"] = self._get_parent_context(node)
            all_entities[entity_name] = entity

        for child in node.children:
            self._extract_all_entities(child, all_entities, depth + 1)

    def _filter_top_level_declarations(self, all_entities: dict) -> None:
        for entity_name, entity_data in all_entities.items():
            if self._is_actually_top_level(entity_data):
                node_obj = self._create_node_from_entity(entity_data)
                if node_obj and self._should_include_node(node_obj):
                    self.nodes.append(node_obj)
                    self.top_level_nodes[entity_name] = node_obj

                    if entity_data["type"] in ["class_declaration", "abstract_class_declaration"]:
                        self._extract_constructor_dependencies(entity_data["node"], entity_name)

    def _is_actually_top_level(self, entity_data: dict) -> bool:
        node = entity_data.get("node")
        if not node or not node.parent:
            return True

        if self._is_inside_function_body(node):
            return False

        current = node.parent
        while current:
            parent_type = current.type

            if parent_type == "program":
                return True

            if parent_type == "export_statement":
                return True

            if parent_type == "ambient_declaration":
                return True

            if parent_type == "module":
                return True

            if parent_type == "statement_block":
                grandparent = current.parent
                if grandparent and grandparent.type in ["module", "ambient_declaration"]:
                    return True

            current = current.parent

        return False

    def _is_inside_function_body(self, node) -> bool:
        current = node.parent
        while current:
            if current.type == "statement_block":
                if current.parent and current.parent.type in [
                    "function_declaration",
                    "generator_function_declaration",
                    "arrow_function",
                    "function_expression",
                    "method_definition",
                ]:
                    return True
            current = current.parent
        return False

    def _extract_ambient_declaration_entity(self, node, depth: int) -> dict:
        name = ""
        for child in node.children:
            if child.type == "module":
                for grandchild in child.children:
                    if grandchild.type == "string":
                        name = self._get_node_text(grandchild).strip("'\"")
                        break
                break
            elif child.type == "namespace":
                name = (
                    self._get_node_text(child.children[1])
                    if len(child.children) > 1
                    else "unknown_namespace"
                )
                break

        return {
            "name": f"{name}",
            "type": "ambient_declaration",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "parameters": [],
            "return_type": None,
            "modifiers": ["ambient"],
            "complexity": 1,
        }

    def _get_parent_context(self, node) -> str:
        """Get the parent context of a node for better top-level detection"""
        if not node.parent:
            return "root"

        parent_type = node.parent.type
        if parent_type in ["program", "source_file"]:
            return "program"
        elif parent_type == "export_statement":
            return "export"
        elif parent_type == "ambient_declaration":
            return "ambient"
        elif parent_type == "module":
            return "module"
        elif parent_type == "statement_block":
            if node.parent.parent and node.parent.parent.type in ["module", "ambient_declaration"]:
                return "module_block"
            return "statement_block"

    def _extract_function_entity(self, node, func_type: str, depth: int) -> dict:
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        func_name = self._get_node_text(name_node)
        parameters = self._extract_parameters(node)
        code_snippet = self._get_node_text(node)

        is_async = (
            "async" in code_snippet.split("function")[0] if "function" in code_snippet else False
        )
        display_name = f"{'async ' if is_async else ''}{func_type} {func_name}"

        return {
            "name": func_name,
            "type": "function",
            "subtype": func_type,
            "parameters": parameters,
            "code_snippet": code_snippet,
            "display_name": display_name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "is_async": is_async,
        }

    def _extract_arrow_function_entity(self, node, depth: int) -> dict:
        """Extract arrow function"""
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            name_node = self._find_child_by_type(parent, "identifier")
            if name_node:
                func_name = self._get_node_text(name_node)
                parameters = self._extract_parameters(node)
                code_snippet = self._get_node_text(parent)

                is_async = "async" in code_snippet.split("=")[0] if "=" in code_snippet else False
                display_name = f"{'async ' if is_async else ''}arrow function {func_name}"

                return {
                    "name": func_name,
                    "type": "function",
                    "subtype": "arrow_function",
                    "parameters": parameters,
                    "code_snippet": code_snippet,
                    "display_name": display_name,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "is_async": is_async,
                }
        return None

    def _extract_method_entity(self, node, depth: int) -> dict:
        """Extract method entity (at any depth), qualified by its class."""
        name_node = self._find_child_by_type(node, "property_identifier")
        if not name_node:
            return None

        method_name = self._get_node_text(name_node)
        class_name = self._enclosing_class_name(node)
        qualified = f"{class_name}.{method_name}" if class_name else method_name
        parameters = self._extract_parameters(node)
        code_snippet = self._get_node_text(node)

        is_async = "async" in code_snippet
        is_static = "static" in code_snippet

        display_name = (
            f"{'static ' if is_static else ''}{'async ' if is_async else ''}method {qualified}"
        )

        return {
            "name": qualified,
            "type": "function",
            "subtype": "method",
            "class_name": class_name,
            "parameters": parameters,
            "code_snippet": code_snippet,
            "display_name": display_name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "is_async": is_async,
            "is_static": is_static,
        }

    def _enclosing_class_name(self, node) -> Optional[str]:
        current = node.parent
        while current:
            if current.type in ("class_declaration", "abstract_class_declaration"):
                name_node = self._find_child_by_type(
                    current, "type_identifier"
                ) or self._find_child_by_type(current, "identifier")
                return self._get_node_text(name_node) if name_node else None
            current = current.parent
        return None

    def _extract_class_entity(self, node, class_type: str, depth: int) -> dict:
        name_node = self._find_child_by_type(node, "type_identifier") or self._find_child_by_type(
            node, "identifier"
        )
        if not name_node:
            return None

        class_name = self._get_node_text(name_node)
        base_classes = self._extract_inheritance(node)
        code_snippet = self._get_node_text(node)

        display_name = f"{class_type} {class_name}"
        if base_classes:
            display_name += f" extends {', '.join(base_classes)}"

        return {
            "name": class_name,
            "type": "class",
            "subtype": class_type,
            "base_classes": base_classes,
            "code_snippet": code_snippet,
            "display_name": display_name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }

    def _extract_interface_entity(self, node, depth: int) -> dict:
        name_node = self._find_child_by_type(node, "type_identifier")
        if not name_node:
            return None

        interface_name = self._get_node_text(name_node)
        base_classes = self._extract_inheritance(node)
        code_snippet = self._get_node_text(node)

        display_name = f"interface {interface_name}"
        if base_classes:
            display_name += f" extends {', '.join(base_classes)}"

        return {
            "name": interface_name,
            "type": "interface",
            "subtype": "interface",
            "base_classes": base_classes,
            "code_snippet": code_snippet,
            "display_name": display_name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }

    def _extract_type_alias_entity(self, node, depth: int) -> dict:
        name_node = self._find_child_by_type(node, "type_identifier")
        if not name_node:
            return None

        type_name = self._get_node_text(name_node)
        code_snippet = self._get_node_text(node)

        return {
            "name": type_name,
            "type": "type",
            "subtype": "type_alias",
            "code_snippet": code_snippet,
            "display_name": f"type {type_name}",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }

    def _extract_enum_entity(self, node, depth: int) -> dict:
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        enum_name = self._get_node_text(name_node)
        code_snippet = self._get_node_text(node)

        return {
            "name": enum_name,
            "type": "enum",
            "subtype": "enum",
            "code_snippet": code_snippet,
            "display_name": f"enum {enum_name}",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }

    def _extract_variable_entity(self, node, depth: int) -> dict:
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        var_name = self._get_node_text(name_node)
        code_snippet = self._get_node_text(node)

        has_function = self._find_child_by_type(node, "arrow_function") or self._find_child_by_type(
            node, "function_expression"
        )

        return {
            "name": var_name,
            "type": "variable",
            "subtype": "variable",
            "code_snippet": code_snippet,
            "display_name": f"variable {var_name}",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "has_function": bool(has_function),
        }

    def _extract_export_statement_entity(self, node, depth: int) -> dict:
        code_snippet = self._get_node_text(node)

        func_decl = self._find_child_by_type(node, "function_declaration")
        class_decl = self._find_child_by_type(node, "class_declaration")
        interface_decl = self._find_child_by_type(node, "interface_declaration")
        lexical_decl = self._find_child_by_type(node, "lexical_declaration")

        if func_decl:
            name_node = self._find_child_by_type(func_decl, "identifier")
            if name_node:
                func_name = self._get_node_text(name_node)
                return {
                    "name": func_name,
                    "type": "function",
                    "subtype": "export_function",
                    "code_snippet": code_snippet,
                    "display_name": f"export function {func_name}",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "parameters": self._extract_parameters(func_decl),
                    "is_export": True,
                }
        elif class_decl:
            name_node = self._find_child_by_type(class_decl, "type_identifier")
            if name_node:
                class_name = self._get_node_text(name_node)
                return {
                    "name": class_name,
                    "type": "class",
                    "subtype": "export_class",
                    "code_snippet": code_snippet,
                    "display_name": f"export class {class_name}",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "base_classes": self._extract_inheritance(class_decl),
                    "is_export": True,
                }
        elif interface_decl:
            name_node = self._find_child_by_type(interface_decl, "type_identifier")
            if name_node:
                interface_name = self._get_node_text(name_node)
                return {
                    "name": interface_name,
                    "type": "interface",
                    "subtype": "export_interface",
                    "code_snippet": code_snippet,
                    "display_name": f"export interface {interface_name}",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "base_classes": self._extract_inheritance(interface_decl),
                    "is_export": True,
                }
        elif lexical_decl:
            var_declarator = self._find_child_by_type(lexical_decl, "variable_declarator")
            if var_declarator:
                name_node = self._find_child_by_type(var_declarator, "identifier")
                func_expr = self._find_child_by_type(
                    var_declarator, "arrow_function"
                ) or self._find_child_by_type(var_declarator, "function_expression")
                if name_node and func_expr:
                    var_name = self._get_node_text(name_node)
                    return {
                        "name": var_name,
                        "type": "function",
                        "subtype": "export_arrow_function",
                        "code_snippet": code_snippet,
                        "display_name": f"export const {var_name}",
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "parameters": self._extract_parameters(func_expr),
                        "is_export": True,
                    }

        default_keyword = None
        call_expr = None
        for child in node.children:
            if child.type == "default":
                default_keyword = child
            elif child.type == "call_expression":
                call_expr = child

        if default_keyword and call_expr:
            callee = call_expr.children[0] if call_expr.children else None
            if callee:
                callee_name = self._get_node_text(callee)
                return {
                    "name": callee_name,
                    "type": "function",
                    "subtype": "export_default_call",
                    "code_snippet": code_snippet,
                    "display_name": f"export default {callee_name}(...)",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "parameters": [],
                    "is_export": True,
                }

        return None

    def _extract_lexical_declaration_entity(self, node, depth: int) -> dict:
        """Extract lexical declaration entity (const/let)."""
        # Find the variable declarator
        var_declarator = self._find_child_by_type(node, "variable_declarator")
        if not var_declarator:
            return None

        name_node = self._find_child_by_type(var_declarator, "identifier")
        if not name_node:
            return None

        var_name = self._get_node_text(name_node)
        code_snippet = self._get_node_text(node)

        # Check declaration type (const/let)
        decl_type = "const" if "const" in code_snippet else "let"

        has_function = self._find_child_by_type(
            var_declarator, "arrow_function"
        ) or self._find_child_by_type(var_declarator, "function_expression")

        return {
            "name": var_name,
            "type": "variable",
            "subtype": f"{decl_type}_declaration",
            "code_snippet": code_snippet,
            "display_name": f"{decl_type} {var_name}",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "has_function": bool(has_function),
            "declaration_type": decl_type,
        }

    def _extract_variable_declaration_entity(self, node, depth: int) -> dict:
        var_declarator = self._find_child_by_type(node, "variable_declarator")
        if not var_declarator:
            return None

        name_node = self._find_child_by_type(var_declarator, "identifier")
        if not name_node:
            return None

        var_name = self._get_node_text(name_node)
        code_snippet = self._get_node_text(node)

        has_function = self._find_child_by_type(
            var_declarator, "arrow_function"
        ) or self._find_child_by_type(var_declarator, "function_expression")

        return {
            "name": var_name,
            "type": "variable",
            "subtype": "var_declaration",
            "code_snippet": code_snippet,
            "display_name": f"var {var_name}",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "has_function": bool(has_function),
            "declaration_type": "var",
        }

    def _create_node_from_entity(self, entity_data: dict) -> Optional[Node]:
        """Create Node object from entity data."""
        try:
            component_type = entity_data["type"]
            name = entity_data["name"]
            node_type = entity_data.get("subtype", entity_data["type"])

            component_id = self._get_component_id(name)
            relative_path = self._get_relative_path()

            return Node(
                id=component_id,
                name=name,
                component_type=component_type,
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code=entity_data["code_snippet"],
                start_line=entity_data["start_line"],
                end_line=entity_data["end_line"],
                has_docstring=False,
                docstring="",
                parameters=entity_data.get("parameters", []),
                node_type=node_type,
                base_classes=entity_data.get("base_classes"),
                class_name=entity_data.get("class_name"),
                display_name=entity_data["display_name"],
                component_id=component_id,
                language="typescript",
                qualified_name=name,
            )
        except Exception as e:
            logger.debug(f"Error creating node from entity: {e}")
            return None

    def _should_include_node(self, node: Node) -> bool:
        excluded_names = {"constructor", "__proto__", "prototype"}

        if node.component_type == "variable":
            return False

        return node.name.split(".")[-1].lower() not in excluded_names

    def _extract_constructor_dependencies(self, class_node, class_name: str) -> None:
        """Extract dependencies from constructor parameters."""
        try:
            class_body = self._find_child_by_type(class_node, "class_body")
            if not class_body:
                return

            for child in class_body.children:
                if child.type == "method_definition":
                    property_name = self._find_child_by_type(child, "property_identifier")
                    if property_name and self._get_node_text(property_name) == "constructor":
                        # Extract parameter types
                        formal_params = self._find_child_by_type(child, "formal_parameters")
                        if formal_params:
                            self._extract_parameter_dependencies(formal_params, class_name)
                        break
        except Exception as e:
            logger.debug(f"Error extracting constructor dependencies: {e}")

    def _extract_parameter_dependencies(self, formal_params, caller_name: str) -> None:
        try:
            for child in formal_params.children:
                if child.type in ["required_parameter", "optional_parameter"]:
                    type_annotation = self._find_child_by_type(child, "type_annotation")
                    if type_annotation:
                        type_id = self._find_child_by_type(type_annotation, "type_identifier")
                        if type_id:
                            dependency_name = self._get_node_text(type_id)
                            if dependency_name and dependency_name != caller_name:
                                self._add_relationship(
                                    caller_name,
                                    dependency_name,
                                    child.start_point[0] + 1,
                                    resolved=dependency_name in self.top_level_nodes,
                                )
        except Exception as e:
            logger.debug(f"Error extracting parameter dependencies: {e}")

    def _get_module_path(self) -> str:
        if self.repo_path:
            try:
                rel_path = os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                rel_path = str(self.file_path)
        else:
            rel_path = str(self.file_path)

        for ext in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]:
            if rel_path.endswith(ext):
                rel_path = rel_path[: -len(ext)]
                break
        return rel_path.replace("/", ".").replace("\\", ".")

    def _get_relative_path(self) -> str:
        if self.repo_path:
            try:
                return os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                return str(self.file_path)
        else:
            return str(self.file_path)

    def _get_component_id(self, name: str) -> str:
        return f"{self._get_relative_path()}::{name}"

    def _extract_inheritance(self, node) -> List[str]:
        """Extract inheritance/implementation relationships."""
        base_classes = []

        extends_clause = self._find_child_by_type(node, "extends_clause")
        if extends_clause:
            for child in extends_clause.children:
                if child.type in ["identifier", "type_identifier"]:
                    base_classes.append(self._get_node_text(child))

        implements_clause = self._find_child_by_type(node, "implements_clause")
        if implements_clause:
            for child in implements_clause.children:
                if child.type in ["identifier", "type_identifier"]:
                    base_classes.append(self._get_node_text(child))

        return base_classes

    def _extract_parameters(self, node) -> List[str]:
        parameters = []
        params_node = self._find_child_by_type(node, "formal_parameters")
        if params_node:
            for child in params_node.children:
                if child.type in ["identifier", "required_parameter", "optional_parameter"]:
                    if child.type == "identifier":
                        parameters.append(self._get_node_text(child))
                    else:
                        param_name = self._find_child_by_type(child, "identifier")
                        if param_name:
                            parameters.append(self._get_node_text(param_name))
        return parameters

    def _extract_all_relationships(self, node, all_entities: dict) -> None:
        self._traverse_for_relationships(node, all_entities, current_top_level=None)

    def _traverse_for_relationships(
        self, node, all_entities: dict, current_top_level: str = None
    ) -> None:
        if current_top_level is None or self._is_new_top_level(node):
            new_top_level = self._get_top_level_name(node)
            if new_top_level and new_top_level in self.top_level_nodes:
                current_top_level = new_top_level

        if current_top_level:
            if node.type == "call_expression":
                self._extract_call_relationship(node, current_top_level, all_entities)
            elif node.type == "new_expression":
                self._extract_new_relationship(node, current_top_level, all_entities)

            elif node.type == "type_annotation":
                self._extract_type_relationship(node, current_top_level, all_entities)
            elif node.type == "type_arguments":
                self._extract_type_arguments_relationship(node, current_top_level, all_entities)

            elif node.type == "extends_clause":
                self._extract_inheritance_relationship(node, current_top_level, all_entities)
            elif node.type == "implements_clause":
                self._extract_inheritance_relationship(node, current_top_level, all_entities)

        for child in node.children:
            self._traverse_for_relationships(child, all_entities, current_top_level)

    def _is_new_top_level(self, node) -> bool:
        return node.type in [
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "export_statement",
            "method_definition",
        ]

    def _get_top_level_name(self, node) -> Optional[str]:
        result = None
        if node.type == "method_definition":
            name_node = self._find_child_by_type(node, "property_identifier")
            if name_node:
                method_name = self._get_node_text(name_node)
                class_name = self._enclosing_class_name(node)
                result = f"{class_name}.{method_name}" if class_name else method_name
        elif node.type in ["function_declaration", "generator_function_declaration"]:
            name_node = self._find_child_by_type(node, "identifier")
            result = self._get_node_text(name_node) if name_node else None
        elif node.type in [
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "type_alias_declaration",
        ]:
            name_node = self._find_child_by_type(
                node, "type_identifier"
            ) or self._find_child_by_type(node, "identifier")
            result = self._get_node_text(name_node) if name_node else None
        elif node.type == "enum_declaration":
            name_node = self._find_child_by_type(node, "identifier")
            result = self._get_node_text(name_node) if name_node else None
        elif node.type == "export_statement":
            if self._find_child_by_type(node, "default"):
                call_expr = self._find_child_by_type(node, "call_expression")
                if call_expr:
                    identifier = self._find_child_by_type(call_expr, "identifier")
                    if identifier:
                        return self._get_node_text(identifier)
                return "default_export"
            else:
                func_decl = self._find_child_by_type(node, "function_declaration")
                class_decl = self._find_child_by_type(node, "class_declaration")
                lexical_decl = self._find_child_by_type(node, "lexical_declaration")

                if func_decl:
                    name_node = self._find_child_by_type(func_decl, "identifier")
                    if name_node:
                        result = self._get_node_text(name_node)
                elif class_decl:
                    name_node = self._find_child_by_type(class_decl, "type_identifier")
                    if name_node:
                        result = self._get_node_text(name_node)
                elif lexical_decl:
                    var_declarator = self._find_child_by_type(lexical_decl, "variable_declarator")
                    if var_declarator:
                        name_node = self._find_child_by_type(var_declarator, "identifier")
                        if name_node:
                            result = self._get_node_text(name_node)
                else:
                    result = "unnamed_export"
        elif node.type in ["lexical_declaration", "variable_declaration"]:
            # const/let/var declarations
            var_declarator = self._find_child_by_type(node, "variable_declarator")
            if var_declarator:
                name_node = self._find_child_by_type(var_declarator, "identifier")
                result = self._get_node_text(name_node) if name_node else None
            else:
                result = None
        else:
            result = None

        return result

    def _extract_call_relationship(self, node, caller_name: str, all_entities: dict) -> None:
        """Emit at most one relationship per call expression.

        Plain identifier calls resolve against same-file components; member
        calls are resolved receiver-first (this/super against the enclosing
        class, class names and typed variables against their methods).
        Unresolved callees are emitted as bare logical names for the global
        resolver, never file-prefixed.
        """
        try:
            call_line = node.start_point[0] + 1
            func_node = node.children[0] if node.children else None
            if func_node is None:
                return

            if func_node.type == "identifier":
                name = self._get_node_text(func_node)
                if not name:
                    return
                if name in self.top_level_nodes:
                    self._add_relationship(caller_name, name, call_line, resolved=True)
                elif name in all_entities and not self._is_actually_top_level(all_entities[name]):
                    logger.debug(f"Ignoring nested call: {caller_name} -> {name} (local/nested)")
                else:
                    self._add_relationship(caller_name, name, call_line, resolved=False)
                return

            if func_node.type != "member_expression":
                return

            parts = self._member_call_parts(func_node)
            if parts is None:
                return
            receiver_kind, receiver_text, tail = parts
            if not tail:
                return

            if receiver_kind in ("this", "super"):
                self._emit_instance_method_call(
                    caller_name, tail, call_line, prefer_bases=(receiver_kind == "super")
                )
            elif receiver_kind == "identifier":
                self._emit_receiver_method_call(node, caller_name, receiver_text, tail, call_line)
            elif receiver_kind == "chain":
                self._add_relationship(
                    caller_name, f"{receiver_text}.{tail}", call_line, resolved=False
                )
            else:
                # Composite or literal receiver: a builtin-prototype method on
                # an unknowable receiver can never resolve to a project
                # component; anything else stays an honest bare name.
                if receiver_kind == "composite" and tail not in JS_TS_PROTOTYPE_METHODS:
                    self._add_relationship(caller_name, tail, call_line, resolved=False)

        except Exception as e:
            logger.debug(f"Error extracting call relationship: {e}")

    def _member_call_parts(self, member_node) -> Optional[Tuple[str, Optional[str], str]]:
        """Split a member expression callee into (receiver_kind, receiver_text, method_tail).

        receiver_kind is one of: "this", "super", "identifier", "chain" (a
        pure dotted identifier chain), "composite" (call result, subscript,
        parenthesized, new expression), or "literal".
        """
        property_node = member_node.child_by_field_name("property") or self._find_child_by_type(
            member_node, "property_identifier"
        )
        tail = self._get_node_text(property_node) if property_node else ""
        obj = member_node.child_by_field_name("object")
        if obj is None and member_node.children:
            obj = member_node.children[0]
        if obj is None:
            return None

        if obj.type == "this":
            return ("this", None, tail)
        if obj.type == "super":
            return ("super", None, tail)
        if obj.type == "identifier":
            return ("identifier", self._get_node_text(obj), tail)
        if obj.type == "member_expression":
            chain = self._identifier_chain(obj)
            if chain:
                return ("chain", chain, tail)
            return ("composite", None, tail)
        if obj.type in (
            "array",
            "string",
            "template_string",
            "number",
            "object",
            "regex",
            "true",
            "false",
        ):
            return ("literal", None, tail)
        return ("composite", None, tail)

    def _identifier_chain(self, node) -> Optional[str]:
        """Flatten a member expression into dotted text when it is a pure
        identifier chain; None when any link is computed."""
        parts = []
        current = node
        while current.type == "member_expression":
            property_node = current.child_by_field_name("property")
            if property_node is None or property_node.type != "property_identifier":
                return None
            parts.append(self._get_node_text(property_node))
            current = current.child_by_field_name("object")
            if current is None:
                return None
        if current.type != "identifier":
            return None
        parts.append(self._get_node_text(current))
        return ".".join(reversed(parts))

    def _emit_instance_method_call(
        self, caller_name: str, tail: str, call_line: int, prefer_bases: bool = False
    ) -> None:
        """Resolve this.m()/super.m() against the enclosing class and its bases."""
        class_name = caller_name.split(".")[0] if caller_name else None
        if not class_name:
            self._add_relationship(caller_name, tail, call_line, resolved=False)
            return

        candidates = [] if prefer_bases else [class_name]
        class_node = self.top_level_nodes.get(class_name)
        if class_node and class_node.base_classes:
            candidates.extend(class_node.base_classes)

        for candidate in candidates:
            if f"{candidate}.{tail}" in self.top_level_nodes:
                self._add_relationship(caller_name, f"{candidate}.{tail}", call_line, resolved=True)
                return

        # Not defined in this file: emit the qualified form for the global
        # resolver (an inherited method defined in another file).
        target = candidates[0] if candidates else class_name
        self._add_relationship(caller_name, f"{target}.{tail}", call_line, resolved=False)

    def _emit_receiver_method_call(
        self, call_node, caller_name: str, receiver: str, tail: str, call_line: int
    ) -> None:
        """Resolve recv.m() where recv is a simple identifier."""
        if not receiver:
            return
        # Static call or enum/namespace access on a known component.
        if receiver in self.top_level_nodes:
            if f"{receiver}.{tail}" in self.top_level_nodes:
                self._add_relationship(caller_name, f"{receiver}.{tail}", call_line, resolved=True)
            else:
                self._add_relationship(caller_name, f"{receiver}.{tail}", call_line, resolved=False)
            return

        inferred = self._infer_identifier_type(call_node, receiver)
        if inferred:
            if f"{inferred}.{tail}" in self.top_level_nodes:
                self._add_relationship(caller_name, f"{inferred}.{tail}", call_line, resolved=True)
            else:
                self._add_relationship(caller_name, f"{inferred}.{tail}", call_line, resolved=False)
            return

        self._add_relationship(caller_name, f"{receiver}.{tail}", call_line, resolved=False)

    def _infer_identifier_type(self, call_node, identifier: str) -> Optional[str]:
        """Find the type of a local identifier from `new X()` initializers or
        TS type annotations within the enclosing function/method/class."""
        scope = call_node.parent
        scopes = []
        while scope:
            if scope.type in (
                "method_definition",
                "function_declaration",
                "generator_function_declaration",
                "arrow_function",
                "function_expression",
                "class_declaration",
                "abstract_class_declaration",
                "program",
            ):
                scopes.append(scope)
            scope = scope.parent

        for scope_node in scopes:
            found = self._find_declared_type(scope_node, identifier)
            if found:
                return found
        return None

    def _find_declared_type(self, scope_node, identifier: str) -> Optional[str]:
        stack = [scope_node]
        while stack:
            current = stack.pop()
            if current.type == "variable_declarator":
                name_node = self._find_child_by_type(current, "identifier")
                if name_node is not None and self._get_node_text(name_node) == identifier:
                    new_expr = self._find_child_by_type(current, "new_expression")
                    if new_expr is not None:
                        for child in new_expr.children:
                            if child.type in ("identifier", "type_identifier"):
                                return self._get_node_text(child)
                    annotation = self._find_child_by_type(current, "type_annotation")
                    if annotation is not None:
                        type_id = self._find_child_by_type(annotation, "type_identifier")
                        if type_id is not None:
                            return self._get_node_text(type_id)
            elif current.type in ("required_parameter", "optional_parameter"):
                name_node = self._find_child_by_type(current, "identifier")
                if name_node is not None and self._get_node_text(name_node) == identifier:
                    annotation = self._find_child_by_type(current, "type_annotation")
                    if annotation is not None:
                        type_id = self._find_child_by_type(annotation, "type_identifier")
                        if type_id is not None:
                            return self._get_node_text(type_id)
            stack.extend(current.children)
        return None

    def _extract_new_relationship(self, node, caller_name: str, all_entities: dict) -> None:
        try:
            call_line = node.start_point[0] + 1
            if node.children:
                constructor_node = None
                for child in node.children:
                    if child.type not in ["new", "type_arguments", "arguments"]:
                        constructor_node = child
                        break

                if constructor_node:
                    constructor_name = self._get_node_text(constructor_node)

                    if constructor_name:
                        resolved = constructor_name in self.top_level_nodes
                        self._add_relationship(
                            caller_name, constructor_name, call_line, resolved=resolved
                        )

        except Exception as e:
            logger.debug(f"Error extracting new relationship: {e}")

    def _extract_type_relationship(self, node, caller_name: str, all_entities: dict) -> None:
        try:
            type_identifiers = []
            self._find_all_type_identifiers(node, type_identifiers)

            call_line = node.start_point[0] + 1

            for type_node in type_identifiers:
                type_name = self._get_node_text(type_node)

                if self._is_builtin_type(type_name):
                    continue

                if type_name in all_entities:
                    target_name = self._resolve_to_top_level(type_name, all_entities)
                    if target_name and target_name in self.top_level_nodes:
                        self._add_relationship(caller_name, target_name, call_line, resolved=True)
                else:
                    self._add_relationship(caller_name, type_name, call_line, resolved=False)

        except Exception as e:
            logger.debug(f"Error extracting type relationship: {e}")

    def _find_all_type_identifiers(self, node, type_identifiers: list) -> None:
        if node.type == "type_identifier":
            type_identifiers.append(node)

        for child in node.children:
            self._find_all_type_identifiers(child, type_identifiers)

    def _extract_type_arguments_relationship(
        self, node, caller_name: str, all_entities: dict
    ) -> None:
        try:
            for child in node.children:
                if child.type == "type_identifier":
                    type_name = self._get_node_text(child)
                    if type_name in all_entities:
                        target_name = self._resolve_to_top_level(type_name, all_entities)
                        if target_name and target_name in self.top_level_nodes:
                            call_line = node.start_point[0] + 1
                            self._add_relationship(
                                caller_name, target_name, call_line, resolved=True
                            )
        except Exception as e:
            logger.debug(f"Error extracting type arguments relationship: {e}")

    def _extract_inheritance_relationship(self, node, caller_name: str, all_entities: dict) -> None:
        """Extract inheritance/implementation relationships"""
        try:
            for child in node.children:
                if child.type in ["identifier", "type_identifier"]:
                    base_name = self._get_node_text(child)
                    if base_name in all_entities:
                        target_name = self._resolve_to_top_level(base_name, all_entities)
                        if target_name and target_name in self.top_level_nodes:
                            call_line = node.start_point[0] + 1
                            self._add_relationship(
                                caller_name, target_name, call_line, resolved=True
                            )
                    else:
                        self._add_relationship(
                            caller_name,
                            base_name,
                            call_line=node.start_point[0] + 1,
                            resolved=False,
                        )
        except Exception as e:
            logger.debug(f"Error extracting inheritance relationship: {e}")

    def _resolve_to_top_level(self, entity_name: str, all_entities: dict) -> Optional[str]:
        if entity_name in self.top_level_nodes:
            return entity_name

        entity_data = all_entities.get(entity_name)
        if entity_data and entity_data.get("depth", 0) > 2:
            return None

        return entity_name if entity_name in self.top_level_nodes else None

    def _add_relationship(
        self, caller_name: str, callee_name: str, call_line: int, resolved: bool = False
    ) -> None:
        """Record one relationship. Resolved callees are component ids in this
        file; unresolved callees stay bare logical names so they never look
        like project components they are not."""
        caller_id = f"{self._get_relative_path()}::{caller_name}"
        callee_id = self._get_component_id(callee_name) if resolved else callee_name

        key = (caller_id, callee_id)
        if key in self.seen_relationships:
            return
        self.seen_relationships.add(key)

        relationship = CallRelationship(
            caller=caller_id,
            callee=callee_id,
            call_line=call_line,
            is_resolved=resolved,
        )
        self.call_relationships.append(relationship)

    def _is_builtin_type(self, name: str) -> bool:
        """Check if type name is a TypeScript/JavaScript built-in type."""
        builtin_types = {
            # Primitive types
            "string",
            "number",
            "boolean",
            "object",
            "undefined",
            "null",
            "void",
            "never",
            "any",
            "unknown",
        }
        return name in builtin_types

    def _is_builtin_function(self, name: str) -> bool:
        builtins = {}
        return name in builtins

    def _find_child_by_type(self, node, node_type: str):
        for child in node.children:
            if child.type == node_type:
                return child
        return None

    def _get_node_text(self, node) -> str:
        start_byte = node.start_byte
        end_byte = node.end_byte
        return self.content.encode("utf8")[start_byte:end_byte].decode("utf8")


def analyze_typescript_file_treesitter(
    file_path: str, content: str, repo_path: str = None
) -> Tuple[List[Node], List[CallRelationship]]:
    try:
        logger.debug(f"Tree-sitter TS analysis for {file_path}")
        analyzer = TreeSitterTSAnalyzer(file_path, content, repo_path)
        analyzer.analyze()
        logger.debug(
            f"Found {len(analyzer.nodes)} top-level nodes, {len(analyzer.call_relationships)} calls"
        )
        return analyzer.nodes, analyzer.call_relationships
    except Exception as e:
        logger.error(f"Error in tree-sitter TS analysis for {file_path}: {e}", exc_info=True)
        return [], []
