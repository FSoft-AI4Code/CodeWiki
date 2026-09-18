"""
PHP language analyzer using tree-sitter-php.

Extracts classes, interfaces, traits, enums, functions, and methods from PHP files,
along with their dependency relationships (use, extends, implements, new, static calls).
"""

import logging
from typing import List, Optional, Tuple, Dict, Set
from pathlib import Path
import os

from tree_sitter import Parser, Language
import tree_sitter_php
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship

logger = logging.getLogger(__name__)

# PHP primitive and built-in types to exclude from dependencies
PHP_PRIMITIVES: Set[str] = {
    "string",
    "int",
    "float",
    "bool",
    "array",
    "object",
    "callable",
    "iterable",
    "mixed",
    "void",
    "null",
    "false",
    "true",
    "never",
    "self",
    "static",
    "parent",
    "integer",
    "boolean",
    "double",
    # Common PHP classes that are built-in
    "Exception",
    "Error",
    "Throwable",
    "Closure",
    "Generator",
    "Iterator",
    "IteratorAggregate",
    "Traversable",
    "ArrayAccess",
    "Serializable",
    "Countable",
    "JsonSerializable",
    "Stringable",
    "DateTime",
    "DateTimeInterface",
    "DateTimeImmutable",
    "DateInterval",
    "stdClass",
    "ArrayObject",
    "SplObjectStorage",
    "WeakReference",
}

# Template file patterns to skip
TEMPLATE_PATTERNS: Set[str] = {".blade.php", ".phtml", ".twig.php"}
TEMPLATE_DIRECTORIES: Set[str] = {"views", "templates", "resources/views"}

# Maximum recursion depth to prevent stack overflow
MAX_RECURSION_DEPTH = 100


class NamespaceResolver:
    """Resolves PHP class names to fully qualified names using use statements."""

    def __init__(self):
        self.current_namespace: str = ""
        self.use_map: Dict[str, str] = {}  # alias -> fully_qualified_name

    def register_namespace(self, ns: str):
        """Set the current namespace."""
        self.current_namespace = ns.replace("\\\\", "\\")

    def register_use(self, fqn: str, alias: str = None):
        """Register a use statement with optional alias."""
        fqn = fqn.replace("\\\\", "\\").lstrip("\\")
        alias = alias or fqn.split("\\")[-1]
        self.use_map[alias] = fqn

    def resolve(self, name: str) -> str:
        """Resolve a name to its fully qualified form."""
        if not name:
            return name

        name = name.replace("\\\\", "\\")

        # Already fully qualified
        if name.startswith("\\"):
            return name[1:]

        # Check use map for alias
        if name in self.use_map:
            return self.use_map[name]

        # Check if first part is an alias (for partial qualified names)
        parts = name.split("\\")
        if parts[0] in self.use_map:
            base = self.use_map[parts[0]]
            if len(parts) > 1:
                return f"{base}\\{'\\'.join(parts[1:])}"
            return base

        # Prepend current namespace
        if self.current_namespace:
            return f"{self.current_namespace}\\{name}"

        return name


class TreeSitterPHPAnalyzer:
    """Analyzes PHP files using tree-sitter to extract nodes and relationships."""

    def __init__(self, file_path: str, content: str, repo_path: str = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []
        self.namespace_resolver = NamespaceResolver()
        self._top_level_nodes: Dict[str, Node] = {}

        # Check if this is a template file that should be skipped
        if self._is_template_file():
            logger.debug(f"Skipping template file: {file_path}")
            return

        self._analyze()

    def _is_template_file(self) -> bool:
        """Check if file is a PHP template that should be skipped."""
        file_str = str(self.file_path)

        # Check extension patterns
        for pattern in TEMPLATE_PATTERNS:
            if file_str.endswith(pattern):
                return True

        # Check directory patterns
        for dir_pattern in TEMPLATE_DIRECTORIES:
            if f"/{dir_pattern}/" in file_str or f"\\{dir_pattern}\\" in file_str:
                return True

        return False

    def _get_module_path(self) -> str:
        """Get module path for the file."""
        if self.repo_path:
            try:
                rel_path = os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                rel_path = str(self.file_path)
        else:
            rel_path = str(self.file_path)

        # Remove .php extension
        for ext in [".php", ".phtml", ".inc"]:
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
        return str(self.file_path)

    def _get_component_id(self, name: str, parent_class: str = None) -> str:
        """Generate component ID for a node."""
        rel_path = self._get_relative_path()
        if parent_class:
            return f"{rel_path}::{parent_class}.{name}"
        return f"{rel_path}::{name}"

    def _analyze(self):
        """Parse and analyze the PHP file."""
        try:
            # Use language_php for mixed PHP/HTML files (most common)
            php_lang_capsule = tree_sitter_php.language_php()
            php_language = Language(php_lang_capsule)
            parser = Parser(php_language)

            tree = parser.parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            # First pass: extract namespace and use statements
            self._extract_namespace_info(root)

            # Second pass: extract nodes
            self._extract_nodes(root, lines, depth=0)

            # Third pass: extract relationships
            self._extract_relationships(root, depth=0)

        except RecursionError:
            logger.warning(f"Max recursion depth exceeded in {self.file_path}")
        except Exception as e:
            logger.error(f"Error parsing PHP file {self.file_path}: {e}")

    def _extract_namespace_info(self, node, depth: int = 0):
        """Extract namespace and use statements from the AST."""
        if depth > MAX_RECURSION_DEPTH:
            return

        if node.type == "namespace_definition":
            # Get namespace name
            name_node = self._find_child_by_type(node, "namespace_name")
            if name_node:
                self.namespace_resolver.register_namespace(name_node.text.decode())

        elif node.type == "namespace_use_declaration":
            self._extract_use_statement(node)

        for child in node.children:
            self._extract_namespace_info(child, depth + 1)

    def _extract_use_statement(self, node):
        """Extract use statement(s) from a namespace_use_declaration node."""
        # Handle group use: use App\{User, Post};
        group_node = self._find_child_by_type(node, "namespace_use_group")
        if group_node:
            prefix_node = self._find_child_by_type(node, "namespace_name")
            prefix = prefix_node.text.decode() if prefix_node else ""

            for child in group_node.children:
                if child.type == "namespace_use_group_clause":
                    name_node = self._find_child_by_type(child, "namespace_name")
                    alias_node = self._find_child_by_type(child, "namespace_aliasing_clause")

                    if name_node:
                        fqn = (
                            f"{prefix}\\{name_node.text.decode()}"
                            if prefix
                            else name_node.text.decode()
                        )
                        alias = None
                        if alias_node:
                            alias_name = self._find_child_by_type(alias_node, "name")
                            if alias_name:
                                alias = alias_name.text.decode()
                        self.namespace_resolver.register_use(fqn, alias)
        else:
            # Handle simple use: use App\User; or use App\User as U;
            for child in node.children:
                if child.type == "namespace_use_clause":
                    name_node = self._find_child_by_type(
                        child, "qualified_name"
                    ) or self._find_child_by_type(child, "namespace_name")
                    alias_node = self._find_child_by_type(child, "namespace_aliasing_clause")

                    if name_node:
                        fqn = name_node.text.decode()
                        alias = None
                        if alias_node:
                            alias_name = self._find_child_by_type(alias_node, "name")
                            if alias_name:
                                alias = alias_name.text.decode()
                        self.namespace_resolver.register_use(fqn, alias)

    def _extract_nodes(self, node, lines: List[str], depth: int = 0, parent_class: str = None):
        """Extract class, interface, trait, enum, function, and method nodes."""
        if depth > MAX_RECURSION_DEPTH:
            logger.warning(f"Max recursion depth reached in {self.file_path}")
            return

        node_type = None
        node_name = None
        docstring = ""

        # Get preceding docstring (PHPDoc)
        docstring = self._get_preceding_docstring(node, lines)

        if node.type == "class_declaration":
            # Check for abstract class
            is_abstract = any(
                c.type == "abstract_modifier"
                or (c.type == "modifier" and c.text.decode() == "abstract")
                for c in node.children
            )
            node_type = "abstract class" if is_abstract else "class"
            name_node = self._find_child_by_type(node, "name")
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "interface_declaration":
            node_type = "interface"
            name_node = self._find_child_by_type(node, "name")
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "trait_declaration":
            node_type = "trait"
            name_node = self._find_child_by_type(node, "name")
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "enum_declaration":
            node_type = "enum"
            name_node = self._find_child_by_type(node, "name")
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "function_definition":
            node_type = "function"
            name_node = self._find_child_by_type(node, "name")
            node_name = name_node.text.decode() if name_node else None

        elif node.type == "method_declaration":
            node_type = "method"
            name_node = self._find_child_by_type(node, "name")
            if name_node:
                method_name = name_node.text.decode()
                containing_class = parent_class or self._find_containing_class_name(node)
                if containing_class:
                    node_name = f"{containing_class}.{method_name}"
                else:
                    node_name = method_name

        if node_type and node_name:
            component_id = self._get_component_id(node_name)
            relative_path = self._get_relative_path()

            # Extract parameters for functions/methods
            parameters = None
            if node_type in ("function", "method"):
                parameters = self._extract_parameters(node)

            # Extract base classes for classes
            base_classes = None
            if node_type in ("class", "abstract class"):
                base_classes = self._extract_base_classes(node)

            node_obj = Node(
                id=component_id,
                name=node_name,
                component_type=node_type,
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code="\n".join(lines[node.start_point[0] : node.end_point[0] + 1]),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                has_docstring=bool(docstring),
                docstring=docstring,
                parameters=parameters,
                node_type=node_type,
                base_classes=base_classes,
                class_name=parent_class,
                display_name=f"{node_type} {node_name}",
                component_id=component_id,
            )
            self.nodes.append(node_obj)
            self._top_level_nodes[node_name] = node_obj

            # Track current class for method extraction
            if node_type in ("class", "abstract class", "interface", "trait", "enum"):
                parent_class = node_name

        # Recursively process children
        for child in node.children:
            self._extract_nodes(child, lines, depth + 1, parent_class)

    def _extract_relationships(self, node, depth: int = 0):
        """Extract dependency relationships from the AST."""
        if depth > MAX_RECURSION_DEPTH:
            return

        # 1. Use statements (already registered, now create relationships)
        if node.type == "namespace_use_declaration":
            self._add_use_relationships(node)

        # 2. Class inheritance (extends)
        if node.type == "class_declaration":
            class_name = self._get_name_from_node(node)
            base_clause = self._find_child_by_type(node, "base_clause")
            if base_clause and class_name:
                base_name = self._get_type_from_clause(base_clause)
                if base_name and not self._is_primitive(base_name):
                    resolved_base = self.namespace_resolver.resolve(base_name)
                    self.call_relationships.append(
                        CallRelationship(
                            caller=self._get_component_id(class_name),
                            callee=resolved_base.replace("\\", "."),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        # 3. Interface implementation (implements)
        if node.type in ("class_declaration", "enum_declaration"):
            implementer_name = self._get_name_from_node(node)
            interface_clause = self._find_child_by_type(node, "class_interface_clause")
            if interface_clause and implementer_name:
                for child in interface_clause.children:
                    if child.type in ("name", "qualified_name"):
                        interface_name = child.text.decode()
                        if not self._is_primitive(interface_name):
                            resolved_interface = self.namespace_resolver.resolve(interface_name)
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=self._get_component_id(implementer_name),
                                    callee=resolved_interface.replace("\\", "."),
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )

        # 4. Object creation (new)
        if node.type == "object_creation_expression":
            containing_class = self._find_containing_class_name(node)
            type_node = self._find_child_by_type(node, "name") or self._find_child_by_type(
                node, "qualified_name"
            )
            if type_node:
                created_type = type_node.text.decode()
                if not self._is_primitive(created_type) and containing_class:
                    resolved_type = self.namespace_resolver.resolve(created_type)
                    self.call_relationships.append(
                        CallRelationship(
                            caller=self._get_component_id(containing_class),
                            callee=resolved_type.replace("\\", "."),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        # 5. Static method calls (::)
        if node.type == "scoped_call_expression":
            containing_class = self._find_containing_class_name(node)
            scope_node = self._find_child_by_type(node, "name") or self._find_child_by_type(
                node, "qualified_name"
            )
            if scope_node and containing_class:
                target_class = scope_node.text.decode()
                if not self._is_primitive(target_class):
                    resolved_target = self.namespace_resolver.resolve(target_class)
                    self.call_relationships.append(
                        CallRelationship(
                            caller=self._get_component_id(containing_class),
                            callee=resolved_target.replace("\\", "."),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        # 6. Property promotion in constructor (PHP 8+)
        if node.type == "property_promotion_parameter":
            containing_class = self._find_containing_class_name(node)
            type_node = self._find_child_by_type(node, "type_list") or self._find_child_by_type(
                node, "named_type"
            )
            if type_node and containing_class:
                type_name = self._extract_type_name(type_node)
                if type_name and not self._is_primitive(type_name):
                    resolved_type = self.namespace_resolver.resolve(type_name)
                    self.call_relationships.append(
                        CallRelationship(
                            caller=self._get_component_id(containing_class),
                            callee=resolved_type.replace("\\", "."),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        # Recursively process children
        for child in node.children:
            self._extract_relationships(child, depth + 1)

    def _add_use_relationships(self, node):
        """Add relationships for use statements."""
        # Get all use clauses from the declaration
        for child in node.children:
            if child.type == "namespace_use_clause":
                name_node = self._find_child_by_type(
                    child, "qualified_name"
                ) or self._find_child_by_type(child, "namespace_name")
                if name_node:
                    fqn = name_node.text.decode().replace("\\", ".")
                    # Add relationship from file to imported class
                    file_id = self._get_relative_path()
                    self.call_relationships.append(
                        CallRelationship(
                            caller=file_id,
                            callee=fqn,
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )
            elif child.type == "namespace_use_group":
                prefix_node = self._find_child_by_type(node, "namespace_name")
                prefix = prefix_node.text.decode() if prefix_node else ""

                for group_child in child.children:
                    if group_child.type == "namespace_use_group_clause":
                        name_node = self._find_child_by_type(group_child, "namespace_name")
                        if name_node:
                            fqn = (
                                f"{prefix}\\{name_node.text.decode()}"
                                if prefix
                                else name_node.text.decode()
                            )
                            file_id = self._get_relative_path()
                            self.call_relationships.append(
                                CallRelationship(
                                    caller=file_id,
                                    callee=fqn.replace("\\", "."),
                                    call_line=node.start_point[0] + 1,
                                    is_resolved=False,
                                )
                            )

    def _find_child_by_type(self, node, child_type: str):
        """Find first child of a specific type."""
        for child in node.children:
            if child.type == child_type:
                return child
        return None

    def _get_name_from_node(self, node) -> Optional[str]:
        """Get name from a declaration node."""
        name_node = self._find_child_by_type(node, "name")
        return name_node.text.decode() if name_node else None

    def _get_type_from_clause(self, clause_node) -> Optional[str]:
        """Extract type name from a base_clause or interface_clause."""
        for child in clause_node.children:
            if child.type in ("name", "qualified_name"):
                return child.text.decode()
        return None

    def _extract_type_name(self, type_node) -> Optional[str]:
        """Extract type name from a type node."""
        if type_node.type == "named_type":
            name_node = self._find_child_by_type(type_node, "name") or self._find_child_by_type(
                type_node, "qualified_name"
            )
            if name_node:
                return name_node.text.decode()
        elif type_node.type in ("name", "qualified_name"):
            return type_node.text.decode()
        elif type_node.type == "type_list":
            # Get first type from union/intersection
            for child in type_node.children:
                if child.type == "named_type":
                    return self._extract_type_name(child)
        return type_node.text.decode() if hasattr(type_node, "text") else None

    def _find_containing_class_name(self, node) -> Optional[str]:
        """Find the name of the containing class/interface/trait/enum."""
        current = node.parent
        while current:
            if current.type in (
                "class_declaration",
                "interface_declaration",
                "trait_declaration",
                "enum_declaration",
            ):
                name_node = self._find_child_by_type(current, "name")
                if name_node:
                    return name_node.text.decode()
            current = current.parent
        return None

    def _get_preceding_docstring(self, node, lines: List[str]) -> str:
        """Extract PHPDoc comment preceding a node."""
        if node.start_point[0] == 0:
            return ""

        # Look at previous sibling or check lines before
        prev_sibling = node.prev_named_sibling
        if prev_sibling and prev_sibling.type == "comment":
            comment_text = prev_sibling.text.decode()
            if comment_text.startswith("/**"):
                return comment_text

        # Check lines directly before the node
        start_line = node.start_point[0]
        if start_line > 0:
            for i in range(start_line - 1, max(0, start_line - 10), -1):
                line = lines[i].strip() if i < len(lines) else ""
                if line.endswith("*/"):
                    # Found end of docblock, now find start
                    docblock_lines = []
                    for j in range(i, max(0, i - 50), -1):
                        docblock_lines.insert(0, lines[j] if j < len(lines) else "")
                        if "/**" in (lines[j] if j < len(lines) else ""):
                            return "\n".join(docblock_lines)
                elif line and not line.startswith("*") and not line.startswith("/**"):
                    break

        return ""

    def _extract_parameters(self, node) -> Optional[List[str]]:
        """Extract function/method parameters as list of strings."""
        params_node = self._find_child_by_type(node, "formal_parameters")
        if params_node:
            params = []
            for child in params_node.children:
                if child.type in (
                    "simple_parameter",
                    "property_promotion_parameter",
                    "variadic_parameter",
                ):
                    # Get the variable name
                    var_node = self._find_child_by_type(child, "variable_name")
                    if var_node:
                        param_text = var_node.text.decode()
                        # Get type if present
                        type_node = self._find_child_by_type(
                            child, "named_type"
                        ) or self._find_child_by_type(child, "primitive_type")
                        if type_node:
                            param_text = f"{type_node.text.decode()} {param_text}"
                        params.append(param_text)
            return params if params else None
        return None

    def _extract_base_classes(self, node) -> Optional[List[str]]:
        """Extract base class names from a class declaration."""
        base_classes = []

        base_clause = self._find_child_by_type(node, "base_clause")
        if base_clause:
            for child in base_clause.children:
                if child.type in ("name", "qualified_name"):
                    base_classes.append(child.text.decode())

        interface_clause = self._find_child_by_type(node, "class_interface_clause")
        if interface_clause:
            for child in interface_clause.children:
                if child.type in ("name", "qualified_name"):
                    base_classes.append(child.text.decode())

        return base_classes if base_classes else None

    def _is_primitive(self, type_name: str) -> bool:
        """Check if type is a PHP primitive or built-in type."""
        if not type_name:
            return True
        # Remove leading backslash and check
        clean_name = type_name.lstrip("\\").split("\\")[-1]
        return clean_name.lower() in {p.lower() for p in PHP_PRIMITIVES}


def analyze_php_file(
    file_path: str, content: str, repo_path: str = None
) -> Tuple[List[Node], List[CallRelationship]]:
    """
    Analyze a PHP file and extract nodes and call relationships.

    Args:
        file_path: Path to the PHP file
        content: Content of the PHP file
        repo_path: Optional path to the repository root

    Returns:
        Tuple of (nodes, call_relationships)
    """
    analyzer = TreeSitterPHPAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
