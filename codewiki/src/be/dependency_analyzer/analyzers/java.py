import logging
from typing import List, Tuple
from pathlib import Path
import os
import re

from tree_sitter import Parser, Language
import tree_sitter_java
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    JAVA_OBJECT_METHODS,
    is_external_symbol,
)

logger = logging.getLogger(__name__)


class TreeSitterJavaAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []
        self.package_name = self._extract_package_name()
        self.import_map, self.wildcard_imports = self._extract_imports()
        self._analyze()

    def _get_module_path(self) -> str:
        if self.repo_path:
            try:
                rel_path = os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                rel_path = str(self.file_path)
        else:
            rel_path = str(self.file_path)

        for ext in [".java"]:
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

    def _get_component_id(self, name: str, parent_class: str = None) -> str:
        rel_path = self._get_relative_path()
        if parent_class:
            return f"{rel_path}::{parent_class}.{name}"
        else:
            return f"{rel_path}::{name}"

    def _extract_package_name(self) -> str:
        match = re.search(r"^\s*package\s+([\w.]+)\s*;", self.content, re.MULTILINE)
        return match.group(1) if match else ""

    def _extract_imports(self) -> tuple[dict[str, str], list[str]]:
        import_map: dict[str, str] = {}
        wildcards: list[str] = []
        for match in re.finditer(
            r"^\s*import\s+(?:static\s+)?([\w.]+)(\.\*)?\s*;", self.content, re.MULTILINE
        ):
            import_name = match.group(1)
            if match.group(2):
                wildcards.append(import_name)
            else:
                import_map[import_name.rsplit(".", 1)[-1]] = import_name
        return import_map, wildcards

    def _analyze(self):
        language_capsule = tree_sitter_java.language()
        java_language = Language(language_capsule)
        parser = Parser(java_language)
        tree = parser.parse(bytes(self.content, "utf8"))
        root = tree.root_node
        lines = self.content.splitlines()

        top_level_nodes = {}

        self._extract_nodes(root, top_level_nodes, lines)

        self._extract_relationships(root, top_level_nodes)

    def _extract_nodes(self, node, top_level_nodes, lines):
        node_type = None
        node_name = None
        qualified_name = None
        class_name = None

        if node.type == "class_declaration":
            is_abstract = any(
                c.type == "modifier" and c.text.decode() == "abstract" for c in node.children
            )
            node_type = "abstract class" if is_abstract else "class"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None
            qualified_name = self._qualified_type_name(
                node_name, self._find_containing_type_names(node)
            )
        elif node.type == "interface_declaration":
            node_type = "interface"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None
            qualified_name = self._qualified_type_name(
                node_name, self._find_containing_type_names(node)
            )
        elif node.type == "enum_declaration":
            node_type = "enum"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None
            qualified_name = self._qualified_type_name(
                node_name, self._find_containing_type_names(node)
            )
        elif node.type == "record_declaration":
            node_type = "record"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None
            qualified_name = self._qualified_type_name(
                node_name, self._find_containing_type_names(node)
            )
        elif node.type == "annotation_type_declaration":
            node_type = "annotation"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            node_name = name_node.text.decode() if name_node else None
            qualified_name = self._qualified_type_name(
                node_name, self._find_containing_type_names(node)
            )
        elif node.type == "method_declaration":
            node_type = "method"
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node:
                method_name = name_node.text.decode()
                containing_types = self._find_containing_type_names(node)
                if containing_types:
                    class_name = containing_types[-1]
                    node_name = f"{class_name}.{method_name}"
                    qualified_name = self._qualified_member_name(containing_types, method_name)
                else:
                    node_name = method_name
                    qualified_name = self._qualify_name(method_name)

        if node_type and node_name:
            component_id = self._get_component_id(node_name)
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
                class_name=class_name,
                display_name=f"{node_type} {node_name}",
                component_id=component_id,
                language="java",
                qualified_name=qualified_name,
            )
            self.nodes.append(node_obj)
            top_level_nodes[node_name] = node_obj
            top_level_nodes[component_id] = node_obj
            if qualified_name:
                top_level_nodes[qualified_name] = node_obj
                top_level_nodes.setdefault(qualified_name.split(".")[-1], node_obj)

        # Recursively process children
        for child in node.children:
            self._extract_nodes(child, top_level_nodes, lines)

    def _extract_relationships(self, node, top_level_nodes):
        # 1. Inheritance: Class extends another class
        if node.type == "class_declaration":
            class_name = self._get_identifier_name(node)

            extends_node = next((c for c in node.children if c.type == "superclass"), None)
            if extends_node:
                base_class_name = self._get_type_name(extends_node)
                if class_name and base_class_name and not self._skip_type(base_class_name, node):
                    caller_id = self._get_component_id(class_name)
                    callee_id = self._resolve_java_type(base_class_name, node, top_level_nodes)
                    self.call_relationships.append(
                        CallRelationship(
                            caller=caller_id,
                            callee=callee_id,
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )
            else:
                logger.debug(f"   No superclass found for {class_name}")

        # 2. Interface Implementation: Class/enum/record implements interface
        if node.type in ["class_declaration", "enum_declaration", "record_declaration"]:
            implementer_name = self._get_identifier_name(node)
            implements_node = next((c for c in node.children if c.type == "super_interfaces"), None)
            if implements_node and implementer_name:
                for child in implements_node.children:
                    if child.type == "type_list":
                        for type_child in child.children:
                            if type_child.type in ["type_identifier", "generic_type"]:
                                interface_name = self._get_type_name(type_child)
                                if interface_name and not self._skip_type(interface_name, node):
                                    caller_id = self._get_component_id(implementer_name)
                                    callee_id = self._resolve_java_type(
                                        interface_name, node, top_level_nodes
                                    )
                                    self.call_relationships.append(
                                        CallRelationship(
                                            caller=caller_id,
                                            callee=callee_id,
                                            call_line=node.start_point[0] + 1,
                                            is_resolved=False,
                                        )
                                    )

        # 3. Field Type Use: Class has field of another class/interface type
        if node.type == "field_declaration":
            containing_class = self._find_containing_class(node, top_level_nodes)
            type_node = next(
                (c for c in node.children if c.type in ["type_identifier", "generic_type"]), None
            )
            if containing_class and type_node:
                field_type_name = self._get_type_name(type_node)
                if field_type_name and not self._skip_type(field_type_name, node):
                    self.call_relationships.append(
                        CallRelationship(
                            caller=containing_class,
                            callee=self._resolve_java_type(field_type_name, node, top_level_nodes),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        # 4. Method Calls: Method calls on objects
        if node.type == "method_invocation":
            containing_class = self._find_containing_class(node, top_level_nodes)
            containing_method = self._find_containing_method(node)
            if containing_class:
                object_name = None
                method_name = None

                identifiers = [
                    child.text.decode() for child in node.children if child.type == "identifier"
                ]
                if len(identifiers) >= 2:
                    object_name = identifiers[0]
                    method_name = identifiers[1]
                elif identifiers:
                    method_name = identifiers[0]

                if method_name:
                    target_type = None

                    caller_id = containing_method or containing_class

                    if object_name and object_name[:1].isupper() and object_name in top_level_nodes:
                        target_type = object_name
                    elif object_name:
                        target_type = self._find_variable_type(node, object_name, top_level_nodes)
                        if not target_type and object_name in top_level_nodes:
                            target_type = object_name
                        if (
                            not target_type
                            and object_name[:1].isupper()
                            and not object_name.isupper()
                        ):
                            # CamelCase receiver with no matching variable reads
                            # as a static call on a type from another file or an
                            # import; ALL_CAPS receivers are constants, not types.
                            target_type = object_name

                    if target_type and not self._skip_type(target_type, node):
                        callee = self._resolve_java_member(
                            method_name, node, top_level_nodes, target_type
                        )
                        if callee not in top_level_nodes and method_name in JAVA_OBJECT_METHODS:
                            # Inherited java.lang.Object method that the project
                            # type does not override locally — never a project edge.
                            callee = None
                        if callee:
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=caller_id,
                                    callee=callee,
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )
                    elif not object_name:
                        callee = self._resolve_java_member(method_name, node, top_level_nodes)
                        if callee in top_level_nodes or self.import_map.get(method_name) == callee:
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=caller_id,
                                    callee=callee,
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )

        # 5. Object Creation
        if node.type == "object_creation_expression":
            containing_class = self._find_containing_class(node, top_level_nodes)
            type_node = next(
                (c for c in node.children if c.type in ["type_identifier", "generic_type"]), None
            )
            if containing_class and type_node:
                created_type = self._get_type_name(type_node)
                if created_type and not self._skip_type(created_type, node):
                    self.call_relationships.append(
                        CallRelationship(
                            caller=containing_class,
                            callee=self._resolve_java_type(created_type, node, top_level_nodes),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        # Recursively process children
        for child in node.children:
            self._extract_relationships(child, top_level_nodes)

    def _is_primitive_type(self, type_name: str) -> bool:
        """Check if type is a Java primitive or a JDK/runtime type."""
        primitives = {
            "boolean",
            "byte",
            "char",
            "double",
            "float",
            "int",
            "long",
            "short",
            "void",
            "var",
        }
        simple = self._simple_type_name(type_name)
        if simple in primitives:
            return True
        # Resolve through the import map first so a runtime type written with its
        # simple name (imported from a `javax.*`/`java.*` package) is judged by its
        # fully-qualified origin. The prefix rules in is_external_symbol then
        # filter JDK/runtime packages, while project types — including sibling
        # packages like `com.other.Bar` — fall through and resolve cross-file. This
        # generalizes JDK filtering to any repository without enumerating types.
        # java.lang types (no import to consult) are covered by the curated set
        # inside is_external_symbol.
        qualified = self.import_map.get(simple)
        if qualified is None:
            # A wildcard import of a JDK package (`import java.util.*;`) is the
            # only way a JDK type outside java.lang appears with no explicit
            # import; project wildcard packages fall through to resolution.
            for wildcard in self.wildcard_imports:
                if is_external_symbol("java", f"{wildcard}.{simple}"):
                    return True
            qualified = simple
        return is_external_symbol("java", qualified)

    def _resolve_java_type(self, type_name: str, context_node=None, top_level_nodes=None) -> str:
        if not type_name:
            return type_name
        type_name = self._simple_type_name(type_name)
        if "." in type_name:
            return type_name
        if type_name in self.import_map:
            return self.import_map[type_name]
        if context_node is not None and top_level_nodes is not None:
            containing_types = self._find_containing_type_names(context_node)
            for idx in range(len(containing_types), 0, -1):
                candidate = self._qualify_name(".".join([*containing_types[:idx], type_name]))
                if candidate in top_level_nodes:
                    return candidate
        if self.package_name:
            return f"{self.package_name}.{type_name}"
        return type_name

    def _resolve_java_member(
        self, member_name: str, context_node, top_level_nodes, target_type: str = None
    ) -> str:
        if target_type:
            qualified_type = self._resolve_java_type(target_type, context_node, top_level_nodes)
            candidate = f"{qualified_type}.{member_name}"
            if candidate in top_level_nodes:
                return candidate
            simple_type = qualified_type.split(".")[-1]
            simple_candidate = f"{simple_type}.{member_name}"
            if simple_candidate in top_level_nodes:
                return simple_candidate
            return candidate

        containing_types = self._find_containing_type_names(context_node)
        for idx in range(len(containing_types), 0, -1):
            candidate = self._qualified_member_name(containing_types[:idx], member_name)
            if candidate in top_level_nodes:
                return candidate
        # A static import maps the bare call to its declaring type, whether
        # project (`com.foo.Util.checkNotNull`) or JDK (`java.util.Objects.requireNonNull`).
        if member_name in self.import_map:
            return self.import_map[member_name]
        return self._qualify_name(member_name)

    def _skip_type(self, type_name: str, context_node) -> bool:
        """Types that can never be project components: primitives, JDK/runtime
        types, and generic type parameters in scope (e.g. the `K`/`V` of an
        enclosing `class Cache<K, V>`)."""
        if self._is_primitive_type(type_name):
            return True
        return self._simple_type_name(type_name) in self._find_type_parameters(context_node)

    def _find_type_parameters(self, node) -> set:
        params = set()
        current = node
        while current:
            if current.type in [
                "class_declaration",
                "interface_declaration",
                "record_declaration",
                "method_declaration",
            ]:
                type_parameters = next(
                    (c for c in current.children if c.type == "type_parameters"), None
                )
                if type_parameters:
                    for param in type_parameters.children:
                        if param.type == "type_parameter":
                            for child in param.children:
                                if child.type in ["type_identifier", "identifier"]:
                                    params.add(child.text.decode())
                                    break
            current = current.parent
        return params

    def _simple_type_name(self, type_name: str) -> str:
        return type_name.strip().split("<", 1)[0].strip()

    def _qualify_name(self, name: str) -> str:
        return f"{self.package_name}.{name}" if self.package_name else name

    def _qualified_type_name(self, name: str, containing_types: list[str]) -> str:
        parts = [*containing_types, name] if name else containing_types
        return self._qualify_name(".".join(parts)) if parts else ""

    def _qualified_member_name(self, containing_types: list[str], member_name: str) -> str:
        return self._qualify_name(".".join([*containing_types, member_name]))

    def _get_identifier_name(self, node):
        """Get identifier name from a node."""
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        return name_node.text.decode() if name_node else None

    def _get_type_name(self, node):
        """Get type name from a type node."""
        if node.type == "type_identifier":
            return node.text.decode()
        elif node.type == "generic_type":
            type_node = next((c for c in node.children if c.type == "type_identifier"), None)
            return type_node.text.decode() if type_node else None
        elif node.type == "superclass":
            type_node = next((c for c in node.children if c.type == "type_identifier"), None)
            return type_node.text.decode() if type_node else None
        return None

    def _find_containing_class(self, node, top_level_nodes):
        current = node.parent
        while current:
            if current.type in [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "annotation_type_declaration",
            ]:
                class_name = self._get_identifier_name(current)
                if class_name and class_name in top_level_nodes:
                    return self._get_component_id(class_name)
            current = current.parent
        return None

    def _find_variable_type(self, node, variable_name, top_level_nodes):
        method_node = node.parent
        while method_node and method_node.type not in [
            "method_declaration",
            "constructor_declaration",
        ]:
            method_node = method_node.parent

        if method_node:
            for child in method_node.children:
                if child.type == "block" or child.type == "constructor_body":
                    variable_type = self._search_variable_declaration(child, variable_name)
                    if variable_type:
                        return variable_type
                elif child.type == "formal_parameters":
                    for param in child.children:
                        if param.type in ["formal_parameter", "spread_parameter"]:
                            type_node = next(
                                (
                                    c
                                    for c in param.children
                                    if c.type in ["type_identifier", "generic_type"]
                                ),
                                None,
                            )
                            identifier_node = next(
                                (c for c in param.children if c.type == "identifier"), None
                            )
                            if (
                                type_node
                                and identifier_node
                                and identifier_node.text.decode() == variable_name
                            ):
                                return self._get_type_name(type_node)

        class_node = node.parent
        while class_node and class_node.type != "class_declaration":
            class_node = class_node.parent

        if class_node:
            for child in class_node.children:
                if child.type == "class_body":
                    for body_child in child.children:
                        if body_child.type == "field_declaration":
                            identifier_node = None
                            type_node = None
                            for field_child in body_child.children:
                                if field_child.type in ["type_identifier", "generic_type"]:
                                    type_node = field_child
                                elif field_child.type == "variable_declarator":
                                    identifier_node = next(
                                        (c for c in field_child.children if c.type == "identifier"),
                                        None,
                                    )

                            if (
                                identifier_node
                                and type_node
                                and identifier_node.text.decode() == variable_name
                            ):
                                field_type = self._get_type_name(type_node)
                                return field_type

        return None

    def _search_variable_declaration(self, block_node, variable_name):
        for child in block_node.children:
            if child.type == "local_variable_declaration":
                type_node = None
                identifier_node = None
                for decl_child in child.children:
                    if decl_child.type in ["type_identifier", "generic_type"]:
                        type_node = decl_child
                    elif decl_child.type == "variable_declarator":
                        identifier_node = next(
                            (c for c in decl_child.children if c.type == "identifier"), None
                        )

                if identifier_node and type_node and identifier_node.text.decode() == variable_name:
                    return self._get_type_name(type_node)

            elif child.type == "block":
                result = self._search_variable_declaration(child, variable_name)
                if result:
                    return result

        return None

    def _find_containing_class_name(self, node):
        names = self._find_containing_type_names(node)
        return names[-1] if names else None

    def _find_containing_type_names(self, node) -> list[str]:
        names = []
        current = node.parent
        while current:
            if current.type in [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "annotation_type_declaration",
            ]:
                name_node = next((c for c in current.children if c.type == "identifier"), None)
                if name_node:
                    names.append(name_node.text.decode())
            current = current.parent
        return list(reversed(names))

    def _find_containing_method(self, node):
        current = node.parent
        while current:
            if current.type == "method_declaration":
                method_name = self._get_identifier_name(current)
                class_name = self._find_containing_class_name(current)
                if method_name and class_name:
                    return self._get_component_id(f"{class_name}.{method_name}")
            current = current.parent
        return None


def analyze_java_file(
    file_path: str, content: str, repo_path: str = None
) -> Tuple[List[Node], List[CallRelationship]]:
    analyzer = TreeSitterJavaAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
