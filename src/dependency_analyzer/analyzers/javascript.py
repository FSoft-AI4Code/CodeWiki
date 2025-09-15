import logging
import os
from typing import List, Set, Optional, Tuple
from pathlib import Path
import sys
import os

from tree_sitter import Parser, Language
import tree_sitter_javascript
import tree_sitter_typescript

from ..models.core import Node, CallRelationship

logger = logging.getLogger(__name__)


class TreeSitterJSAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []
        
        self.top_level_nodes = {}
        
        self.seen_relationships = set()

        try:
            language_capsule = tree_sitter_javascript.language()
            self.js_language = Language(language_capsule)
            self.parser = Parser(self.js_language)

        except Exception as e:
            logger.error(f"Failed to initialize JavaScript parser: {e}")
            self.parser = None
            self.js_language = None


    def _add_relationship(self, relationship: CallRelationship) -> bool:
        rel_key = (relationship.caller, relationship.callee, relationship.call_line)
        
        if rel_key not in self.seen_relationships:
            self.seen_relationships.add(rel_key)
            self.call_relationships.append(relationship)
            return True
        return False

    def analyze(self) -> None:
        if self.parser is None:
            logger.warning(f"Skipping {self.file_path} - parser initialization failed")
            return

        try:
            tree = self.parser.parse(bytes(self.content, "utf8"))
            root_node = tree.root_node

            logger.debug(f"Parsed AST with root node type: {root_node.type}")

            self._extract_functions(root_node)
            self._extract_call_relationships(root_node)

            logger.debug(
                f"Analysis complete: {len(self.nodes)} nodes, {len(self.call_relationships)} relationships"
            )

        except Exception as e:
            logger.error(f"Error analyzing JavaScript file {self.file_path}: {e}", exc_info=True)

    def _get_module_path(self) -> str:
        if self.repo_path:
            try:
                rel_path = os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                rel_path = str(self.file_path)
        else:
            rel_path = str(self.file_path)
        
        for ext in ['.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs']:
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

    def _get_component_id(self, name: str, class_name: str = None, is_method: bool = False) -> str:
        module_path = self._get_module_path()
        
        if is_method and class_name:
            return f"{module_path}.{class_name}.{name}"
        elif class_name and not is_method: 
            return f"{module_path}.{name}"
        else:  
            return f"{module_path}.{name}"

    def _find_containing_class(self, node) -> Optional[str]:
        parent = node.parent
        while parent:
            if parent.type in ["class_declaration", "abstract_class_declaration", "interface_declaration"]:
                name_node = self._find_child_by_type(parent, "type_identifier")
                if not name_node:
                    name_node = self._find_child_by_type(parent, "identifier")
                if name_node:
                    return self._get_node_text(name_node)
            parent = parent.parent
        return None

    def _extract_functions(self, node) -> None:
        self._traverse_for_functions(node)
        self.nodes.sort(key=lambda n: n.start_line)

    def _traverse_for_functions(self, node) -> None:
        if node.type in ["class_declaration", "abstract_class_declaration", "interface_declaration"]:
            cls = self._extract_class_declaration(node)
            if cls:
                self.nodes.append(cls)
                self.top_level_nodes[cls.name] = cls
                
                self._extract_methods_from_class(node, cls.name)
                
        elif node.type == "function_declaration":
            containing_class = self._find_containing_class(node)
            if containing_class is None:
                func = self._extract_function_declaration(node)
                if func and self._should_include_function(func):
                    self.nodes.append(func)
                    self.top_level_nodes[func.name] = func
        elif node.type == "generator_function_declaration":
            containing_class = self._find_containing_class(node)
            if containing_class is None:
                func = self._extract_function_declaration(node)
                if func and self._should_include_function(func):
                    self.nodes.append(func)
                    self.top_level_nodes[func.name] = func
        elif node.type == "export_statement":
            func = self._extract_exported_function(node)
            if func and self._should_include_function(func):
                self.nodes.append(func)
                self.top_level_nodes[func.name] = func
        elif node.type == "lexical_declaration":
            containing_class = self._find_containing_class(node)
            if containing_class is None:
                func = self._extract_arrow_function_from_declaration(node)
                if func and self._should_include_function(func):
                    self.nodes.append(func)
                    self.top_level_nodes[func.name] = func
        
        for child in node.children:
            self._traverse_for_functions(child)

    def _extract_methods_from_class(self, class_node, class_name: str) -> None:
        class_body = self._find_child_by_type(class_node, "class_body")
        if not class_body:
            return
            
        for child in class_body.children:
            if child.type == "method_definition":
                method_name = self._get_method_name(child)
                if method_name:
                    method_key = f"{self._get_module_path()}.{class_name}.{method_name}"
                    method_node = self._create_method_node(child, method_name, class_name)
                    if method_node:
                        self.top_level_nodes[method_key] = method_node
            elif child.type == "field_definition":
                # Handle arrow function properties
                field_name = self._get_field_name(child)
                if field_name and self._is_arrow_function_field(child):
                    method_key = f"{self._get_module_path()}.{class_name}.{field_name}"
                    method_node = self._create_method_node(child, field_name, class_name)
                    if method_node:
                        self.top_level_nodes[method_key] = method_node

    def _get_method_name(self, method_node) -> Optional[str]:
        """Get method name from method_definition node."""
        if method_node.type != "method_definition":
            return None
        
        for child in method_node.children:
            if child.type == "property_identifier":
                return self._get_node_text(child)
        return None

    def _get_field_name(self, field_node) -> Optional[str]:
        """Get field name from field_definition node."""
        if field_node.type != "field_definition":
            return None
            
        for child in field_node.children:
            if child.type == "property_identifier":
                return self._get_node_text(child)
        return None

    def _is_arrow_function_field(self, field_node) -> bool:
        """Check if field_definition contains an arrow function."""
        for child in field_node.children:
            if child.type == "arrow_function":
                return True
        return False

    def _create_method_node(self, node, method_name: str, class_name: str) -> Optional[Node]:
        """Create a method node for relationship mapping."""
        try:
            line_start = node.start_point[0] + 1
            line_end = node.end_point[0] + 1
            component_id = self._get_component_id(method_name, class_name, is_method=True)
            relative_path = self._get_relative_path()
            
            return Node(
                id=component_id,
                name=method_name,
                component_type="method",
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code="\n".join(self.content.splitlines()[line_start - 1 : line_end]),
                start_line=line_start,
                end_line=line_end,
                has_docstring=False,
                docstring="",
                parameters=None,
                node_type="method",
                base_classes=None,
                class_name=class_name,
                display_name=f"method {method_name}",
                component_id=component_id
            )
        except Exception as e:
            logger.debug(f"Error creating method node for {method_name}: {e}")
            return None

    def _extract_class_declaration(self, node) -> Optional[Node]:
        """Extract class/abstract class/interface declaration."""
        try:
            name_node = self._find_child_by_type(node, "type_identifier")
            if not name_node:
                name_node = self._find_child_by_type(node, "identifier")
            if not name_node:
                return None
            name = self._get_node_text(name_node)
            line_start = node.start_point[0] + 1
            line_end = node.end_point[0] + 1
            docstring = None
            base_classes = []
            heritage_node = self._find_child_by_type(node, "class_heritage")
            if heritage_node:
                for child in heritage_node.children:
                    if child.type in ["identifier", "type_identifier"]:
                        base_classes.append(self._get_node_text(child))
            code_snippet = "\n".join(self.content.splitlines()[line_start - 1 : line_end])
            
            if node.type == "abstract_class_declaration":
                node_type = "abstract class"
                display_name = f"abstract class {name}"
            elif node.type == "interface_declaration":
                node_type = "interface"
                display_name = f"interface {name}"
            else:
                node_type = "class"
                display_name = f"class {name}"
            
            component_id = self._get_component_id(name, is_method=False)
            relative_path = self._get_relative_path()
            
            return Node(
                id=component_id,
                name=name,
                component_type=node_type,
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code=code_snippet,
                start_line=line_start,
                end_line=line_end,
                has_docstring=bool(docstring),
                docstring=docstring or "",
                parameters=None,
                node_type=node_type,
                base_classes=base_classes if base_classes else None,
                class_name=None,
                display_name=display_name,
                component_id=component_id,
            )
        except Exception:
            return None

    def _extract_function_declaration(self, node) -> Optional[Node]:
        try:
            name_node = self._find_child_by_type(node, "identifier")
            if not name_node:
                return None

            func_name = self._get_node_text(name_node)
            line_start = node.start_point[0] + 1
            line_end = node.end_point[0] + 1
            parameters = self._extract_parameters(node)
            code_snippet = self._get_node_text(node)

            # Check for async and generator from code snippet
            is_async = "async function" in code_snippet
            is_generator = "function*" in code_snippet or "*" in func_name
            
            if is_async and is_generator:
                display_name = f"async generator {func_name}"
            elif is_async:
                display_name = f"async function {func_name}"
            elif is_generator:
                display_name = f"generator function {func_name}"
            else:
                display_name = f"function {func_name}"

            component_id = self._get_component_id(func_name, is_method=False)
            relative_path = self._get_relative_path()

            return Node(
                id=component_id,
                name=func_name,
                component_type="function",
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code=code_snippet,
                start_line=line_start,
                end_line=line_end,
                has_docstring=False,
                docstring="",
                parameters=parameters,
                node_type="function",
                base_classes=None,
                class_name=None,
                display_name=display_name,
                component_id=component_id,
            )
        except Exception as e:
            logger.debug(f"Error extracting function declaration: {e}")
            return None
    def _extract_exported_function(self, node) -> Optional[Node]:
        """Extract export function or export default function"""
        try:
            func_decl = self._find_child_by_type(node, "function_declaration")
            if func_decl:
                func = self._extract_function_declaration(func_decl)
                if func:
                    export_text = self._get_node_text(node)
                    if "export default" in export_text and "function (" in export_text:
                        func.name = "default"
                return func
        except Exception as e:
            logger.debug(f"Error extracting exported function: {e}")
        return None

    def _extract_arrow_function_from_declaration(self, node) -> Optional[Node]:
        """Extract arrow function or function expression from const/let/var declarations."""
        try:
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = self._find_child_by_type(child, "identifier")
                    func_node = self._find_child_by_type(
                        child, "arrow_function"
                    ) or self._find_child_by_type(child, "function_expression")

                    if name_node and func_node:
                        func_name = self._get_node_text(name_node)
                        line_start = func_node.start_point[0] + 1
                        line_end = func_node.end_point[0] + 1
                        parameters = self._extract_parameters(func_node)
                        code_snippet = self._get_node_text(child)

                        component_id = self._get_component_id(func_name, is_method=False)
                        relative_path = self._get_relative_path()

                        return Node(
                            id=component_id,
                            name=func_name,
                            component_type="function",
                            file_path=str(self.file_path),
                            relative_path=relative_path,
                            source_code=code_snippet,
                            start_line=line_start,
                            end_line=line_end,
                            has_docstring=False,
                            docstring="",
                            parameters=parameters,
                            node_type="function",
                            base_classes=None,
                            class_name=None,
                            display_name=f"function {func_name}",
                            component_id=component_id,
                        )
            return None
        except Exception as e:
            logger.debug(f"Error extracting function from declaration: {e}")
        return None

    def _should_include_function(self, func: Node) -> bool:
        excluded_names = {}

        if func.name.lower() in excluded_names:
            logger.debug(f"Skipping excluded function: {func.name}")
            return False

        return True

    def _extract_parameters(self, node) -> List[str]:
        parameters = []
        params_node = self._find_child_by_type(node, "formal_parameters")
        if params_node:
            for child in params_node.children:
                if child.type == "identifier":
                    parameters.append(self._get_node_text(child))
        return parameters

    def _extract_call_relationships(self, node) -> None:
        current_top_level = None
        self._traverse_for_calls(node, current_top_level)

    def _traverse_for_calls(self, node, current_top_level) -> None:
        if current_top_level:
            self._extract_jsdoc_type_dependencies(node, current_top_level)
        
        if node.type in ["class_declaration", "abstract_class_declaration", "interface_declaration"]:
            name_node = self._find_child_by_type(node, "type_identifier") or self._find_child_by_type(node, "identifier")
            if name_node:
                current_top_level = self._get_node_text(name_node)
                
                heritage_node = self._find_child_by_type(node, "class_heritage")
                if heritage_node:
                    for child in heritage_node.children:
                        if child.type in ["identifier", "type_identifier"]:
                            base_class = self._get_node_text(child)
                            caller_id = self._get_component_id(current_top_level)
                            callee_id = f"{self._get_module_path()}.{base_class}" 
                            inheritance_rel = CallRelationship(
                                caller=caller_id,
                                callee=callee_id,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False
                            )
                            self._add_relationship(inheritance_rel)
                            
        elif node.type == "function_declaration":
            name_node = self._find_child_by_type(node, "identifier")
            if name_node:
                current_top_level = self._get_node_text(name_node)
        elif node.type == "generator_function_declaration":
            name_node = self._find_child_by_type(node, "identifier")
            if name_node:
                current_top_level = self._get_node_text(name_node)
        elif node.type == "lexical_declaration":
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = self._find_child_by_type(child, "identifier")
                    func_node = self._find_child_by_type(child, "arrow_function") or self._find_child_by_type(child, "function_expression")
                    if name_node and func_node:
                        current_top_level = self._get_node_text(name_node)

        if node.type == "call_expression" and current_top_level:
            call_info = self._extract_call_from_node(node, current_top_level)
            if call_info:
                self._add_relationship(call_info)
        
        elif node.type == "await_expression" and current_top_level:
            call_expr = self._find_child_by_type(node, "call_expression")
            if call_expr:
                call_info = self._extract_call_from_node(call_expr, current_top_level)
                if call_info:
                    self._add_relationship(call_info)
        
        elif node.type == "new_expression" and current_top_level:
            callee_name = self._extract_callee_name(node)
            if callee_name:
                call_info = CallRelationship(
                    caller=f"{self._get_module_path()}.{current_top_level}",
                    callee=f"{self._get_module_path()}.{callee_name}",
                    call_line=node.start_point[0] + 1,
                    is_resolved=False
                )
                self._add_relationship(call_info)

        for child in node.children:
            self._traverse_for_calls(child, current_top_level)

    def _extract_call_from_node(self, node, caller_name: str) -> Optional[CallRelationship]:
        """Extract call relationship from a call_expression node."""
        try:
            call_line = node.start_point[0] + 1
            callee_name = self._extract_callee_name(node)
            
            if not callee_name:
                return None
            
            call_text = self._get_node_text(node)
            is_method_call = "this." in call_text or "super." in call_text
            
            caller_id = f"{self._get_module_path()}.{caller_name}"
            
            if is_method_call:
                current_class = None
                for node_key, node_obj in self.top_level_nodes.items():
                    if node_obj.component_type == "class" and caller_name in node_key:
                        current_class = node_obj.name
                        break
                
                if current_class:
                    method_key = f"{self._get_module_path()}.{current_class}.{callee_name}"
                    if method_key in self.top_level_nodes:
                        return None
            
            callee_id = f"{self._get_module_path()}.{callee_name}"
            if callee_name in self.top_level_nodes:
                return CallRelationship(
                    caller=caller_id,
                    callee=callee_id,
                    call_line=call_line,
                    is_resolved=True,
                )
            
            return CallRelationship(
                caller=caller_id,
                callee=callee_id,
                call_line=call_line,
                is_resolved=False,
            )
            
        except Exception as e:
            logger.debug(f"Error extracting call relationship: {e}")
            return None

    def _extract_jsdoc_type_dependencies(self, node, caller_name: str) -> None:
        """Extract type dependencies from JSDoc comments."""
        try:
            if hasattr(node, 'prev_sibling') and node.prev_sibling:
                prev = node.prev_sibling
                if prev.type == "comment":
                    comment_text = self._get_node_text(prev)
                    self._parse_jsdoc_types(comment_text, caller_name, node.start_point[0] + 1)
            
            for child in node.children:
                if child.type == "comment":
                    comment_text = self._get_node_text(child)
                    self._parse_jsdoc_types(comment_text, caller_name, node.start_point[0] + 1)
                    
        except Exception as e:
            logger.debug(f"Error extracting JSDoc dependencies: {e}")

    def _parse_jsdoc_types(self, comment_text: str, caller_name: str, line_number: int) -> None:
        """Parse JSDoc comment text and extract type references."""
        import re
        try:
            type_patterns = [
                r'@param\s*\{([^}]+)\}',     # @param {Type}
                r'@returns?\s*\{([^}]+)\}',  # @return {Type} or @returns {Type}
                r'@type\s*\{([^}]+)\}',      # @type {Type}
                r'@typedef\s*\{[^}]*\}\s*(\w+)', # @typedef {Object} TypeName
                r'@interface\s+(\w+)',       # @interface InterfaceName
            ]
            
            for pattern in type_patterns:
                matches = re.findall(pattern, comment_text)
                for match in matches:
                    type_name = match.strip()
                    
                    base_types = self._extract_base_types_from_jsdoc(type_name)
                    
                    for base_type in base_types:
                        if base_type and not self._is_builtin_type_js(base_type):
                            caller_id = f"{self._get_module_path()}.{caller_name}"
                            callee_id = f"{self._get_module_path()}.{base_type}"
                            
                            type_rel = CallRelationship(
                                caller=caller_id,
                                callee=callee_id,
                                call_line=line_number,
                                is_resolved=False 
                            )
                            
                            if self._add_relationship(type_rel):
                                pass
                                    
        except Exception as e:
            logger.debug(f"Error parsing JSDoc types: {e}")

    def _extract_base_types_from_jsdoc(self, type_str: str) -> list:
        import re
        type_str = type_str.strip()
        
        base_types = []
        
        main_type_match = re.match(r'^(\w+)', type_str)
        if main_type_match:
            base_types.append(main_type_match.group(1))
        
        generic_matches = re.findall(r'<([^<>]+)>', type_str)
        for generic in generic_matches:
            subtypes = re.findall(r'\b(\w+)\b', generic)
            base_types.extend(subtypes)
        
        if '|' in type_str:
            union_types = type_str.split('|')
            for union_type in union_types:
                clean_type = re.match(r'\b(\w+)\b', union_type.strip())
                if clean_type:
                    base_types.append(clean_type.group(1))
        
        return base_types

    def _is_builtin_type_js(self, name: str) -> bool:
        """Check if type name is a JavaScript/JSDoc built-in type."""
        builtin_types = {
            # JavaScript primitive types
            "string", "number", "boolean", "object", "undefined", "null", "void", "any",
            
            # Global JavaScript types
            "Array", "Promise", "Date", "RegExp", "Error", "Map", "Set", "WeakMap", "WeakSet",
            "Function", "Object", "String", "Number", "Boolean", "Symbol", "BigInt",
            
            "Element", "HTMLElement", "Document", "Window", "Event", "EventTarget", "Node",
            "Response", "Request", "Headers", "URL", "URLSearchParams", "FormData", "Blob", "File",
            
            # Common JSDoc generic parameters
            "T", "U", "V", "K", "P", "R", "E"
        }
        return name in builtin_types

    def _extract_callee_name(self, call_node) -> Optional[str]:
        if not call_node.children:
            return None
            
        callee_node = call_node.children[0]

        if callee_node.type == "identifier":
            return self._get_node_text(callee_node)
        elif callee_node.type == "member_expression":
            property_node = self._find_child_by_type(callee_node, "property_identifier")
            if property_node:
                return self._get_node_text(property_node)
            
            computed_property = self._find_child_by_type(callee_node, "computed_property_name")
            if computed_property:
                for child in computed_property.children:
                    if child.type == "identifier":
                        return self._get_node_text(child)
        elif callee_node.type == "super":
            return "super"
        elif callee_node.type == "this":
            return "this"
            
        return None

    def _find_child_by_type(self, node, node_type: str):
        """Find first child node of specified type."""
        for child in node.children:
            if child.type == node_type:
                return child
        return None

    def _get_node_text(self, node) -> str:
        start_byte = node.start_byte
        end_byte = node.end_byte
        return self.content.encode("utf8")[start_byte:end_byte].decode("utf8")

    def _find_containing_class_name(self, method_node) -> Optional[str]:
        current = method_node.parent
        while current:
            if current.type == "class_declaration":
                name_node = self._find_child_by_type(current, "identifier")
                if name_node:
                    return self._get_node_text(name_node)
            current = current.parent
        return None

    def _extract_assignment_name(self, node) -> Optional[str]:
        if node.type == "identifier":
            return self._get_node_text(node)
        elif node.type == "member_expression":
            property_node = self._find_child_by_type(node, "property_identifier")
            if property_node:
                return self._get_node_text(property_node)
        return None

def analyze_javascript_file_treesitter(
    file_path: str, content: str, repo_path: str = None
) -> Tuple[List[Node], List[CallRelationship]]:
    """Analyze a JavaScript file using tree-sitter."""
    try:
        logger.debug(f"Tree-sitter JS analysis for {file_path}")
        analyzer = TreeSitterJSAnalyzer(file_path, content, repo_path)
        analyzer.analyze()
        logger.debug(
            f"Found {len(analyzer.nodes)} top-level nodes, {len(analyzer.call_relationships)} calls"
        )
        return analyzer.nodes, analyzer.call_relationships
    except Exception as e:
        logger.error(f"Error in tree-sitter JS analysis for {file_path}: {e}", exc_info=True)
        return [], []




