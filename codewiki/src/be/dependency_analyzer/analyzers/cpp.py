import logging
from typing import List, Optional, Tuple
from pathlib import Path
import os
import re

from tree_sitter import Parser, Language
import tree_sitter_cpp
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    is_external_symbol,
    is_macro_name,
)

logger = logging.getLogger(__name__)

# An ALL_CAPS attribute/specifier macro sitting in front of a declaration, e.g.
# `EXPORT_API void foo()` or `CONSTEXPR auto bar()`. Matched at line start or
# after a structural delimiter so identifiers in expression position are left
# untouched. Name-agnostic: relies on the ALL_CAPS macro naming convention
# rather than any specific library's prefix.
_SPECIFIER_MACRO_RE = re.compile(r"(^\s*|[{};>,]\s*)([A-Z][A-Z0-9_]*[A-Z0-9])(\s+)(?=[A-Za-z_~])")
# Same, but for function-like specifier macros such as `VISIBILITY("default") void f()`.
_SPECIFIER_MACRO_CALL_RE = re.compile(
    r"(^\s*|[{};>,]\s*)([A-Z][A-Z0-9_]*[A-Z0-9])\s*\([^()]*\)(\s+)(?=[A-Za-z_~])"
)
# An export/visibility macro between a class-like keyword and the type name,
# e.g. `class LIB_API logger {`. Without this the macro is taken as the type
# name and the real name is lost.
_KEYWORD_MACRO_RE = re.compile(
    r"\b(class|struct|union|enum)(\s+)([A-Z][A-Z0-9_]*[A-Z0-9])\s+(?=[A-Za-z_~])"
)
# A line that is nothing but a bare ALL_CAPS macro (optionally a macro call),
# e.g. namespace-bracket macros like `LIB_BEGIN_NAMESPACE`. Left in place these
# break parsing of the declaration that follows, so the line is blanked. Begin/
# end pairs are both removed, keeping any braces they expand to balanced.
_STANDALONE_MACRO_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*[A-Z0-9])(\s*\([^()]*\))?\s*$")


class TreeSitterCppAnalyzer:
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

        for ext in [".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hxx", ".h++", ".h"]:
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

    def _get_component_id(self, name: str, parent_class: str = None) -> str:
        rel_path = self._get_relative_path()
        if parent_class:
            return f"{rel_path}::{parent_class}.{name}"
        return f"{rel_path}::{name}"

    def _analyze(self):
        language_capsule = tree_sitter_cpp.language()
        cpp_language = Language(language_capsule)
        parser = Parser(cpp_language)
        root = self._parse_with_macro_recovery(parser)
        lines = self.content.splitlines()

        top_level_nodes = {}

        # collect all top-level nodes using recursive traversal
        self._extract_nodes(root, top_level_nodes, lines)

        # extract relationships between top-level nodes
        self._extract_relationships(root, top_level_nodes)

    def _parse_with_macro_recovery(self, parser):
        """Parse the original source; if it has syntax errors, retry with macro
        normalization and keep whichever parse has fewer errors.

        Normalization strips ALL_CAPS tokens by naming convention, which is
        wrong for code whose *types* are ALL_CAPS (e.g. Win32 `HANDLE`/`DWORD`).
        Comparing error counts makes the heuristic self-correcting: clean files
        are never touched, and normalization is only kept when it demonstrably
        recovers structure.
        """
        tree = parser.parse(bytes(self.content, "utf8"))
        if not tree.root_node.has_error:
            return tree.root_node

        normalized = self._normalize_for_parser(self.content)
        if normalized == self.content:
            return tree.root_node

        normalized_tree = parser.parse(bytes(normalized, "utf8"))
        if self._count_parse_errors(normalized_tree.root_node) < self._count_parse_errors(
            tree.root_node
        ):
            return normalized_tree.root_node
        return tree.root_node

    def _count_parse_errors(self, root) -> int:
        errors = 0
        stack = [root]
        while stack:
            node = stack.pop()
            if node.is_error or node.is_missing:
                errors += 1
            stack.extend(node.children)
        return errors

    def _normalize_for_parser(self, content: str) -> str:
        """Strip ALL_CAPS attribute/specifier macros that sit in front of a
        declaration so tree-sitter can recover the underlying signature. This is
        name-agnostic: it keys off the conventional ALL_CAPS macro spelling, not
        any specific library's prefix, and only fires in specifier position so
        identifiers used in expressions are preserved. Line count is unchanged so
        reported line numbers stay accurate.
        """
        normalized_lines = []
        for line in content.splitlines():
            updated = line
            standalone = _STANDALONE_MACRO_RE.match(updated)
            if standalone and is_macro_name(standalone.group(1)):
                normalized_lines.append("")
                continue
            for pattern in (_SPECIFIER_MACRO_CALL_RE, _SPECIFIER_MACRO_RE):
                previous = None
                while previous != updated:
                    previous = updated
                    updated = pattern.sub(
                        lambda m: (
                            (m.group(1) + m.group(3)) if is_macro_name(m.group(2)) else m.group(0)
                        ),
                        updated,
                    )
            updated = _KEYWORD_MACRO_RE.sub(
                lambda m: (m.group(1) + m.group(2)) if is_macro_name(m.group(3)) else m.group(0),
                updated,
            )
            normalized_lines.append(updated)
        return "\n".join(normalized_lines)

    def _extract_nodes(self, node, top_level_nodes, lines):
        """Recursively extract top-level nodes (classes, functions, global variables)."""
        node_type = None
        node_name = None
        containing_class = None

        if node.type == "class_specifier":
            # "class" + type_identifier + { ... }
            node_type = "class"
            # Find type_identifier that represents the class name
            for child in node.children:
                if child.type == "type_identifier":
                    node_name = child.text.decode()
                    break
        elif node.type == "struct_specifier":
            # "struct" + type_identifier + { ... }
            node_type = "struct"
            # Find type_identifier that represents the struct name
            for child in node.children:
                if child.type == "type_identifier":
                    node_name = child.text.decode()
                    break
        elif node.type == "function_definition":
            # Check if this is inside a class or function
            containing_class = self._find_containing_class_for_method(node)
            declarator = next((c for c in node.children if c.type == "function_declarator"), None)
            qualified_parts = self._get_qualified_declarator_parts(declarator) if declarator else []
            if not containing_class and len(qualified_parts) > 1:
                containing_class = qualified_parts[-2]
            if containing_class:
                node_type = "method"
            else:
                node_type = "function"

            if declarator:
                for child in declarator.children:
                    if child.type == "identifier":
                        node_name = child.text.decode()
                        break
                    elif child.type == "field_identifier":
                        node_name = child.text.decode()
                        break
                    elif child.type == "qualified_identifier":
                        identifiers = [c for c in child.children if c.type == "identifier"]
                        if identifiers:
                            node_name = identifiers[-1].text.decode()
                            break
        elif node.type == "declaration":
            containing_class = self._find_containing_class_for_method(node)
            declarator = next((c for c in node.children if c.type == "function_declarator"), None)
            if containing_class and declarator:
                node_type = "method"
                node_name = self._get_declarator_name(declarator)
            elif self._is_global_variable(node):
                node_type = "variable"
                for child in node.children:
                    if child.type == "init_declarator":
                        identifier = next(
                            (c for c in child.children if c.type == "identifier"), None
                        )
                        if identifier:
                            node_name = identifier.text.decode()
                            break
                    elif child.type == "identifier":
                        node_name = child.text.decode()
                        break
        elif node.type == "alias_declaration":
            # using name = type; — aliases are real API surface (e.g. a
            # library's public alias for an internal template), so they are
            # extracted as components and can resolve call/type references.
            node_type = "type_alias"
            for child in node.children:
                if child.type == "type_identifier":
                    node_name = child.text.decode()
                    break
        elif node.type == "type_definition":
            # typedef ... name; — the alias name is the trailing type_identifier
            node_type = "type_alias"
            identifiers = [c for c in node.children if c.type == "type_identifier"]
            if identifiers:
                node_name = identifiers[-1].text.decode()
        elif node.type == "namespace_definition":
            node_type = "namespace"
            found_namespace_keyword = False
            for child in node.children:
                if child.type == "namespace":
                    found_namespace_keyword = True
                elif found_namespace_keyword and child.type == "identifier":
                    node_name = child.text.decode()
                    break

        if node_type and node_name:
            if node_type == "method":
                component_id = self._get_component_id(node_name, containing_class)
                top_level_key = component_id
            else:
                component_id = self._get_component_id(node_name)
                top_level_key = node_name

            relative_path = self._get_relative_path()

            node_obj = Node(
                id=component_id,
                name=node_name,
                component_type=node_type,
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code="\n".join(lines[node.start_point[0] : node.end_point[0] + 1]),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                has_docstring=False,
                docstring="",
                parameters=None,
                node_type=node_type,
                base_classes=None,
                class_name=containing_class if node_type == "method" else None,
                display_name=f"{node_type} {node_name}",
                component_id=component_id,
                language="cpp",
                qualified_name=f"{containing_class}.{node_name}" if containing_class else node_name,
            )

            top_level_nodes[top_level_key] = node_obj
            top_level_nodes[component_id] = node_obj
            if node_type == "method" and containing_class:
                top_level_nodes[f"{containing_class}.{node_name}"] = node_obj
                top_level_nodes.setdefault(node_name, node_obj)

            if node_type in ["class", "struct", "function", "method", "type_alias"]:
                self.nodes.append(node_obj)

        # Recursively process children
        for child in node.children:
            self._extract_nodes(child, top_level_nodes, lines)

    def _is_global_variable(self, node) -> bool:
        """Check if a declaration node is a global variable."""
        parent = node.parent
        while parent:
            if parent.type in ["function_definition", "class_specifier", "struct_specifier"]:
                return False
            parent = parent.parent
        return True

    def _get_declarator_name(self, declarator) -> Optional[str]:
        """Extract the declared function or method name from nested declarators."""
        for child in declarator.children:
            if child.type in ["identifier", "field_identifier"]:
                return child.text.decode()
            if child.type == "qualified_identifier":
                identifiers = [
                    c for c in child.children if c.type in ["identifier", "field_identifier"]
                ]
                if identifiers:
                    return identifiers[-1].text.decode()
            if child.children:
                name = self._get_declarator_name(child)
                if name:
                    return name
        return None

    def _get_qualified_declarator_parts(self, declarator) -> list[str]:
        if declarator is None:
            return []
        for child in declarator.children:
            if child.type == "qualified_identifier":
                return [
                    c.text.decode()
                    for c in child.children
                    if c.type
                    in ["identifier", "field_identifier", "type_identifier", "namespace_identifier"]
                ]
            if child.children:
                parts = self._get_qualified_declarator_parts(child)
                if parts:
                    return parts
        return []

    def _find_containing_class_for_method(self, node):
        """Find the class that contains this method definition."""
        current = node.parent
        while current:
            if current.type == "class_specifier":
                # Get class name
                for child in current.children:
                    if child.type == "type_identifier":
                        return child.text.decode()
            elif current.type == "struct_specifier":
                # Get struct name
                for child in current.children:
                    if child.type == "type_identifier":
                        return child.text.decode()
            current = current.parent
        return None

    def _extract_relationships(self, node, top_level_nodes):
        if node.type == "call_expression":
            containing_function_id = self._find_containing_function_or_method(node, top_level_nodes)
            if containing_function_id:
                # Get called function name
                called_function = None
                receiver_name = None
                for child in node.children:
                    if child.type == "identifier":
                        called_function = child.text.decode()
                        break
                    elif child.type == "field_expression":
                        receiver_name, method_name = self._get_field_call_parts(child)
                        if method_name:
                            called_function = method_name
                            break

                if called_function:
                    target_method = None
                    if receiver_name:
                        receiver_type = self._find_variable_type(node, receiver_name)
                        if receiver_type:
                            target_method = self._find_method_component(
                                called_function, top_level_nodes, receiver_type
                            )
                    if not target_method:
                        target_method = self._find_method_component(
                            called_function, top_level_nodes
                        )
                    target_class = self._find_class_containing_method(
                        called_function, top_level_nodes
                    )

                    if target_method:
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_function_id,
                                callee=target_method,
                                call_line=node.start_point[0] + 1,
                                is_resolved=True,
                            )
                        )
                    elif target_class:
                        target_class_id = self._get_component_id(target_class)
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_function_id,
                                callee=target_class_id,
                                call_line=node.start_point[0] + 1,
                                is_resolved=True,
                            )
                        )
                    elif called_function in top_level_nodes:
                        called_function_id = top_level_nodes[called_function].id
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_function_id,
                                callee=called_function_id,
                                call_line=node.start_point[0] + 1,
                                is_resolved=True,
                            )
                        )
                    elif receiver_name is not None:
                        # A member call whose receiver type could not be
                        # resolved: a name matching an STL member here is
                        # overwhelmingly likely external, so suppress it.
                        if not self._is_system_function(called_function):
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=containing_function_id,
                                    callee=called_function,
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )
                    elif not is_macro_name(
                        called_function
                    ) and called_function not in self._find_template_parameters(node):
                        # Plain calls are emitted for cross-file resolution;
                        # external filtering happens centrally after the
                        # project resolver has had its chance.
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_function_id,
                                callee=called_function,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

        elif node.type == "base_class_clause":
            # Find the containing class
            containing_class = self._find_containing_class(node)
            if containing_class:
                template_params = self._find_template_parameters(node)
                # Extract base class names
                for child in node.children:
                    if child.type == "type_identifier":
                        base_class = child.text.decode()
                        if base_class in template_params or is_macro_name(base_class):
                            continue
                        containing_class_id = self._get_component_id(containing_class)
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_class_id,
                                callee=base_class,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

        elif node.type == "new_expression":
            containing_function_id = self._find_containing_function_or_method(node, top_level_nodes)
            if containing_function_id:
                # Get the class being instantiated
                for child in node.children:
                    if child.type == "type_identifier":
                        class_name = child.text.decode()
                        if class_name in top_level_nodes:
                            class_id = self._get_component_id(class_name)
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=containing_function_id,
                                    callee=class_id,
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=True,
                                )
                            )
                        break

        elif node.type == "identifier":
            parent = node.parent
            if parent and parent.type not in [
                "function_definition",
                "class_specifier",
                "declaration",
                "function_declarator",
            ]:
                var_name = node.text.decode()
                if (
                    var_name in top_level_nodes
                    and top_level_nodes[var_name].component_type == "variable"
                ):
                    containing_function_id = self._find_containing_function_or_method(
                        node, top_level_nodes
                    )
                    if containing_function_id:
                        self.call_relationships.append(
                            CallRelationship(
                                caller=containing_function_id,
                                callee=var_name,
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

        # Recursively process children
        for child in node.children:
            self._extract_relationships(child, top_level_nodes)

    def _get_field_call_parts(self, field_expression) -> tuple[Optional[str], Optional[str]]:
        receiver_name = None
        method_name = None
        for child in field_expression.children:
            if child.type == "field_identifier":
                method_name = child.text.decode()
            elif child.type == "identifier" and receiver_name is None:
                receiver_name = child.text.decode()
            elif child.type == "field_expression" and receiver_name is None:
                receiver_name = child.text.decode().split(".")[-1].split("->")[-1]
        return receiver_name, method_name

    def _find_variable_type(self, node, variable_name: str) -> Optional[str]:
        current = node.parent
        while current:
            if current.type in ["compound_statement", "field_declaration_list", "translation_unit"]:
                found = self._search_variable_declaration(current, variable_name)
                if found:
                    return found
            if current.type == "function_definition":
                declarator = next(
                    (c for c in current.children if c.type == "function_declarator"), None
                )
                found = self._search_parameter_declaration(declarator, variable_name)
                if found:
                    return found
            current = current.parent
        return None

    def _search_variable_declaration(self, node, variable_name: str) -> Optional[str]:
        for child in node.children:
            if child.type == "declaration":
                type_name = self._get_declaration_type_name(child)
                declared_name = self._get_declared_variable_name(child)
                if declared_name == variable_name:
                    return type_name or self._get_constructor_type_name(child)
            if child.children and child.type not in [
                "class_specifier",
                "struct_specifier",
                "function_definition",
            ]:
                found = self._search_variable_declaration(child, variable_name)
                if found:
                    return found
        return None

    def _search_parameter_declaration(self, node, variable_name: str) -> Optional[str]:
        if node is None:
            return None
        if node.type == "parameter_declaration":
            type_name = self._get_declaration_type_name(node)
            declared_name = self._get_declared_variable_name(node)
            if declared_name == variable_name:
                return type_name
        for child in node.children:
            found = self._search_parameter_declaration(child, variable_name)
            if found:
                return found
        return None

    def _get_declaration_type_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type in ["type_identifier", "primitive_type", "qualified_identifier"]:
                return self._last_type_part(child.text.decode())
            if child.type in ["template_type", "generic_type"]:
                return self._last_type_part(child.text.decode().split("<", 1)[0])
        return None

    def _get_declared_variable_name(self, node) -> Optional[str]:
        for child in reversed(node.children):
            if child.type in ["identifier", "field_identifier"]:
                return child.text.decode()
            if child.type in [
                "init_declarator",
                "pointer_declarator",
                "reference_declarator",
                "array_declarator",
            ]:
                name = self._get_declared_variable_name(child)
                if name:
                    return name
        return None

    def _get_constructor_type_name(self, node) -> Optional[str]:
        for child in node.children:
            if child.type == "call_expression":
                for call_child in child.children:
                    if call_child.type in ["identifier", "type_identifier"]:
                        return call_child.text.decode()
            if child.children:
                found = self._get_constructor_type_name(child)
                if found:
                    return found
        return None

    def _last_type_part(self, type_name: str) -> str:
        return type_name.strip("&* ").split("::")[-1]

    def _find_containing_function(self, node, top_level_nodes):
        """Find the function that contains this node."""
        current = node.parent
        while current:
            if current.type == "function_definition":
                # Get function name
                declarator = next(
                    (c for c in current.children if c.type == "function_declarator"), None
                )
                if declarator:
                    identifier = next(
                        (c for c in declarator.children if c.type == "identifier"), None
                    )
                    if identifier:
                        func_name = identifier.text.decode()
                        if func_name in top_level_nodes:
                            return func_name
            current = current.parent
        return None

    def _find_containing_function_or_method(self, node, top_level_nodes):
        """Find the function or method that contains this node."""
        current = node.parent
        while current:
            if current.type == "function_definition":
                declarator = next(
                    (c for c in current.children if c.type == "function_declarator"), None
                )
                if declarator:
                    func_name = self._get_declarator_name(declarator)
                    if func_name:
                        containing_class = self._find_containing_class_for_method(current)
                        qualified_parts = self._get_qualified_declarator_parts(declarator)
                        if not containing_class and len(qualified_parts) > 1:
                            containing_class = qualified_parts[-2]
                        if containing_class:
                            return self._get_component_id(func_name, containing_class)
                        return self._get_component_id(func_name)
            current = current.parent
        return None

    def _get_component_id_for_function(self, func_name, top_level_nodes):
        if func_name in top_level_nodes:
            node_obj = top_level_nodes[func_name]
            if hasattr(node_obj, "class_name") and node_obj.class_name:
                return self._get_component_id(func_name, node_obj.class_name)
            else:
                return self._get_component_id(func_name)
        return self._get_component_id(func_name)

    def _find_containing_class(self, node):
        """Find the class that contains this node."""
        current = node.parent
        while current:
            if current.type == "class_specifier":
                # Get class name
                for child in current.children:
                    if child.type == "type_identifier":
                        return child.text.decode()
            current = current.parent
        return None

    def _find_template_parameters(self, node) -> set:
        """Collect template type-parameter names in scope at this node, so a
        reference to `T`/`Char`/... is not reported as an unresolved project
        symbol."""
        params = set()
        current = node.parent
        while current:
            if current.type == "template_declaration":
                param_list = next(
                    (c for c in current.children if c.type == "template_parameter_list"), None
                )
                if param_list:
                    for param in param_list.children:
                        for child in getattr(param, "children", []):
                            if child.type == "type_identifier":
                                params.add(child.text.decode())
            current = current.parent
        return params

    def _is_system_function(self, func_name: str) -> bool:
        """Check if a call target is external rather than a project function.

        Besides the curated standard-library set, an ALL_CAPS callee is treated as
        a macro invocation: macros are not extracted as components, so a call to
        one can never resolve to a project function and would otherwise pollute the
        graph as unresolved noise. This only affects the unresolved fallback —
        real components in ALL_CAPS (rare in C++) are matched by the earlier
        resolution branches before this check runs.
        """
        if is_external_symbol("cpp", func_name):
            return True
        return is_macro_name(func_name)

    def _find_method_component(self, method_name, top_level_nodes, class_name: str = None):
        if class_name:
            qualified_key = f"{class_name}.{method_name}"
            if qualified_key in top_level_nodes:
                return top_level_nodes[qualified_key].id
        for node_obj in top_level_nodes.values():
            if node_obj.component_type == "method" and node_obj.name == method_name:
                return node_obj.id
        return None

    def _find_class_containing_method(self, method_name, top_level_nodes):
        for node_name, node_obj in top_level_nodes.items():
            if node_obj.component_type in ["class", "struct"]:
                if self._class_has_method(node_obj, method_name):
                    return node_name
        return None

    def _class_has_method(self, class_node, method_name):
        lines = class_node.source_code.split("\n")
        for line in lines:
            if f"{method_name}(" in line and (
                "void" in line or "int" in line or "bool" in line or class_node.name in line
            ):
                return True
        return False


def analyze_cpp_file(
    file_path: str, content: str, repo_path: str = None
) -> Tuple[List[Node], List[CallRelationship]]:
    analyzer = TreeSitterCppAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
