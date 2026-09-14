"""Tree-sitter based Scala analyzer.

Extracts classes, case classes, traits, objects, package objects, Scala 3
enums, and their methods as documentable components, plus inheritance,
mixin, field/constructor-type, instantiation, and call dependency edges.

Component types follow the two-level `component_type` / `node_type` split
described in openspec/changes/add-scala-language-support/design.md —
Decision 1: `component_type` is the coarse type the pipeline gates leaf
selection on (traits map to "interface", objects to "class" so both stay
leaf-eligible), while `node_type` and `display_name` preserve the faithful
Scala construct.

Companion objects get a `$`-suffixed component id (Decision 2), mirroring
Scala's own JVM encoding of module classes, so a class and its same-named
companion never collide.
"""

import logging
import os
from pathlib import Path

import tree_sitter_scala
from tree_sitter import Language, Parser

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node

logger = logging.getLogger(__name__)

# Node types that introduce a new component scope (owner of members / target
# of extends-clause edges).
_TYPE_DEFINITION_NODES = (
    "class_definition",
    "trait_definition",
    "object_definition",
    "package_object",
    "enum_definition",
)
_METHOD_DEFINITION_NODES = ("function_definition", "function_declaration")

# `component_type` / `node_type` / display-prefix per design.md Decision 1.
_TYPE_MAPPING = {
    "trait_definition": ("interface", "trait"),
    "class_definition": ("class", "class"),
    "object_definition": ("class", "object"),
    "package_object": ("class", "object"),
    "enum_definition": ("class", "enum"),
}

# Scala primitives and common built-in types excluded from dependency edges,
# following the Kotlin and PHP precedent (design.md — Risks/Trade-offs).
SCALA_PRIMITIVE_TYPES = frozenset(
    {
        "Int",
        "Long",
        "Short",
        "Byte",
        "Double",
        "Float",
        "Boolean",
        "Char",
        "String",
        "Unit",
        "Any",
        "AnyRef",
        "AnyVal",
        "Nothing",
        "Null",
        "Object",
        "List",
        "Seq",
        "Vector",
        "Array",
        "Set",
        "Map",
        "Option",
        "Some",
        "None",
        "Either",
        "Left",
        "Right",
        "Try",
        "Success",
        "Failure",
        "Iterable",
        "Iterator",
        "Tuple2",
        "Tuple3",
        "Tuple4",
        "Tuple5",
        "Function0",
        "Function1",
        "Function2",
        "PartialFunction",
    }
)

# Ubiquitous collection/standard-library method names that never point at a
# repository component. Calls whose only signal is one of these names are
# dropped instead of emitted as unresolved relationships.
SCALA_CORE_CALLS = frozenset(
    {
        "map",
        "flatMap",
        "filter",
        "filterNot",
        "foreach",
        "fold",
        "foldLeft",
        "foldRight",
        "reduce",
        "reduceLeft",
        "reduceRight",
        "collect",
        "collectFirst",
        "sortBy",
        "sortWith",
        "sorted",
        "groupBy",
        "partition",
        "zip",
        "zipWithIndex",
        "take",
        "takeWhile",
        "drop",
        "dropWhile",
        "head",
        "headOption",
        "tail",
        "last",
        "lastOption",
        "isEmpty",
        "nonEmpty",
        "size",
        "length",
        "contains",
        "exists",
        "forall",
        "find",
        "sum",
        "min",
        "max",
        "mkString",
        "toList",
        "toSeq",
        "toSet",
        "toMap",
        "toVector",
        "toArray",
        "toString",
        "apply",
        "getOrElse",
        "orElse",
        "get",
        "isDefined",
        "flatten",
        "distinct",
        "reverse",
        "append",
        "prepend",
        "copy",
        "hashCode",
        "equals",
        "println",
        "print",
        "require",
        "assert",
    }
)


class TreeSitterScalaAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        # Same-file symbol table keyed by logical name ("Foo", "Foo$", "Foo.bar").
        self.top_level_nodes: dict = {}
        self.seen_relationships: set = set()
        self._analyze()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _get_relative_path(self) -> str:
        if self.repo_path:
            try:
                return os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                return str(self.file_path)
        return str(self.file_path)

    def _get_component_id(self, logical_name: str) -> str:
        return f"{self._get_relative_path()}::{logical_name}"

    def _analyze(self):
        try:
            scala_language = Language(tree_sitter_scala.language())
            parser = Parser(scala_language)
            tree = parser.parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            self._extract_nodes(root, lines)
            self._extract_relationships(root)
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Scala file {self.file_path}: {e}")

    @staticmethod
    def _field_text(node, field_name: str) -> str | None:
        child = node.child_by_field_name(field_name)
        return child.text.decode() if child is not None else None

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------

    def _owner_key_for_definition(self, def_node) -> str | None:
        """The logical name a class/trait/object/enum is registered under."""
        name = self._field_text(def_node, "name")
        if not name:
            return None
        if def_node.type in ("object_definition", "package_object"):
            return f"{name}$"
        return name

    def _find_containing_type(self, node) -> str | None:
        """Walk up to the nearest enclosing class/trait/object/enum and return
        its owner key (`$`-suffixed for object-kind constructs), or None at
        file scope."""
        current = node.parent
        while current is not None:
            if current.type in _TYPE_DEFINITION_NODES:
                return self._owner_key_for_definition(current)
            current = current.parent
        return None

    def _find_containing_method(self, node) -> str | None:
        """Walk up to the nearest enclosing method/function and return its
        logical (owner-qualified) name, or None."""
        current = node.parent
        while current is not None:
            if current.type in _METHOD_DEFINITION_NODES:
                name = self._field_text(current, "name")
                if name:
                    owner = self._find_containing_type(current)
                    return f"{owner}.{name}" if owner else name
            current = current.parent
        return None

    def _resolve_caller(self, node) -> str | None:
        """Prefer the enclosing method; fall back to the enclosing type for
        expressions in field initializers."""
        method = self._find_containing_method(node)
        if method:
            return self._get_component_id(method)
        owner = self._find_containing_type(node)
        if owner:
            return self._get_component_id(owner)
        return None

    # ------------------------------------------------------------------
    # Pass 1: components
    # ------------------------------------------------------------------

    def _extract_nodes(self, node, lines):
        component_type = None
        node_type = None
        logical_name = None
        class_name = None
        parameters = None
        base_classes = None

        if node.type in _TYPE_DEFINITION_NODES:
            logical_name = self._owner_key_for_definition(node)
            if logical_name:
                component_type, node_type = _TYPE_MAPPING[node.type]
                base_classes = self._extract_extends_types(node)
        elif node.type in _METHOD_DEFINITION_NODES:
            name = self._field_text(node, "name")
            if name:
                parameters = self._extract_parameters(node)
                owner = self._find_containing_type(node)
                if owner:
                    component_type, node_type = "method", "method"
                    logical_name = f"{owner}.{name}"
                    class_name = owner
                else:
                    component_type, node_type = "function", "function"
                    logical_name = name

        if component_type and logical_name:
            self._add_node(
                node,
                logical_name,
                component_type,
                node_type,
                lines,
                class_name=class_name,
                parameters=parameters,
                base_classes=base_classes,
            )

        for child in node.children:
            self._extract_nodes(child, lines)

    def _extract_parameters(self, func_node) -> list[str] | None:
        # A curried method (`def add(x: Int)(y: Int)`) has one `parameters`
        # node per group; child_by_field_name would silently return only the
        # first, so every group is collected via children_by_field_name.
        params_nodes = func_node.children_by_field_name("parameters")
        params = []
        for params_node in params_nodes:
            for child in params_node.children:
                if child.type == "parameter":
                    params.append(child.text.decode().strip())
        return params or None

    def _add_node(
        self,
        node,
        logical_name: str,
        component_type: str,
        node_type: str,
        lines,
        class_name: str | None = None,
        parameters: list[str] | None = None,
        base_classes: list[str] | None = None,
    ):
        component_id = self._get_component_id(logical_name)
        relative_path = self._get_relative_path()

        docstring = ""
        comment = node.prev_sibling
        if comment is not None and comment.type in ("block_comment", "comment"):
            docstring = comment.text.decode().strip()

        start_line_idx = node.start_point[0]
        end_line_idx = node.end_point[0] + 1
        code_snippet = (
            "\n".join(lines[start_line_idx:end_line_idx]) if start_line_idx < len(lines) else ""
        )

        display_name = f"{node_type} {logical_name}"

        node_obj = Node(
            id=component_id,
            name=logical_name,
            component_type=component_type,
            file_path=str(self.file_path),
            relative_path=relative_path,
            source_code=code_snippet,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            has_docstring=bool(docstring),
            docstring=docstring,
            parameters=parameters,
            node_type=node_type,
            base_classes=base_classes,
            class_name=class_name,
            display_name=display_name,
            component_id=component_id,
            language="scala",
        )
        self.nodes.append(node_obj)
        self.top_level_nodes[logical_name] = node_obj

    # ------------------------------------------------------------------
    # Pass 2: relationships
    # ------------------------------------------------------------------

    def _extract_relationships(self, node):
        if node.type in _TYPE_DEFINITION_NODES:
            self._emit_extends_edges(node)
        elif node.type == "class_parameter":
            self._emit_class_parameter_edge(node)
        elif node.type in ("val_definition", "var_definition"):
            self._emit_field_type_edge(node)
        elif node.type == "instance_expression":
            self._emit_instantiation_edge(node)
        elif node.type == "call_expression":
            self._emit_call_edge(node)

        for child in node.children:
            self._extract_relationships(child)

    def _get_type_name(self, node) -> str | None:
        """Get the primary type name from a type node, stripping generics."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return node.text.decode()
        if node.type == "generic_type":
            base = node.child_by_field_name("type")
            return self._get_type_name(base) if base is not None else None
        if node.type == "stable_identifier":
            idents = [c for c in node.children if c.type == "identifier"]
            return idents[-1].text.decode() if idents else None
        return None

    def _extract_extends_types(self, def_node) -> list[str] | None:
        extends_clause = def_node.child_by_field_name("extend")
        if extends_clause is None:
            return None
        types = []
        for child in extends_clause.children:
            if child.type in ("type_identifier", "generic_type", "stable_identifier"):
                name = self._get_type_name(child)
                if name:
                    types.append(name)
        return types or None

    def _emit_extends_edges(self, def_node):
        owner_key = self._owner_key_for_definition(def_node)
        if not owner_key:
            return
        types = self._extract_extends_types(def_node) or []
        if not types:
            return
        caller_id = self._get_component_id(owner_key)
        call_line = def_node.start_point[0] + 1
        for type_name in types:
            if self._is_primitive(type_name):
                continue
            self._add_type_relationship(caller_id, type_name, call_line)

    def _emit_class_parameter_edge(self, param_node):
        class_params = param_node.parent
        def_node = class_params.parent if class_params is not None else None
        # Scala 3 traits can take value parameters too (`trait Foo(x: T)`),
        # sharing the same `class_parameters` shape as classes — restricting
        # this to class_definition would silently drop their edges, even
        # though _find_variable_type already resolves calls through them.
        if def_node is None or def_node.type not in _TYPE_DEFINITION_NODES:
            return
        owner_key = self._owner_key_for_definition(def_node)
        if not owner_key:
            return
        type_node = param_node.child_by_field_name("type")
        type_name = self._get_type_name(type_node)
        if type_name and not self._is_primitive(type_name):
            caller_id = self._get_component_id(owner_key)
            self._add_type_relationship(caller_id, type_name, param_node.start_point[0] + 1)

    def _emit_field_type_edge(self, val_node):
        owner = self._find_containing_type(val_node)
        if not owner:
            return
        type_node = val_node.child_by_field_name("type")
        type_name = self._get_type_name(type_node)
        if type_name and not self._is_primitive(type_name):
            caller_id = self._get_component_id(owner)
            self._add_type_relationship(caller_id, type_name, val_node.start_point[0] + 1)

    def _emit_instantiation_edge(self, inst_node):
        caller_id = self._resolve_caller(inst_node)
        if not caller_id:
            return
        type_node = None
        for child in inst_node.children:
            if child.type in ("type_identifier", "generic_type", "stable_identifier"):
                type_node = child
                break
        type_name = self._get_type_name(type_node)
        if type_name and not self._is_primitive(type_name):
            self._add_type_relationship(caller_id, type_name, inst_node.start_point[0] + 1)

    def _emit_call_edge(self, call_node):
        caller_id = self._resolve_caller(call_node)
        if not caller_id:
            return
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            return
        call_line = call_node.start_point[0] + 1

        if func_node.type == "identifier":
            self._emit_bare_call_edge(caller_id, func_node.text.decode(), call_node, call_line)
        elif func_node.type == "field_expression":
            self._emit_field_call_edge(caller_id, func_node, call_node, call_line)

    def _emit_bare_call_edge(self, caller_id: str, callee_name: str, call_node, call_line: int):
        owner = self._find_containing_type(call_node)
        candidates = []
        if owner:
            candidates.append(f"{owner}.{callee_name}")
        candidates.append(callee_name)
        if callee_name[:1].isupper():
            # Capitalized bare calls are instantiation-shaped: a case-class
            # apply or a companion object's apply.
            candidates.append(f"{callee_name}$")

        resolved_id = self._resolve_candidates(candidates)
        if resolved_id:
            self._add_relationship_raw(caller_id, resolved_id, call_line, True)
            return
        if callee_name[:1].isupper() or not self._is_call_noise(callee_name):
            self._add_relationship_raw(caller_id, callee_name, call_line, False)

    def _emit_field_call_edge(self, caller_id: str, func_node, call_node, call_line: int):
        receiver = func_node.child_by_field_name("value")
        method_node = func_node.child_by_field_name("field")
        if receiver is None or method_node is None:
            return
        method_name = method_node.text.decode()

        if receiver.type != "identifier":
            # Composite receiver (a call chain, literal, ...): keep only the
            # bare method name, and only when it isn't stdlib noise.
            if not self._is_call_noise(method_name):
                self._add_relationship_raw(caller_id, method_name, call_line, False)
            return

        receiver_name = receiver.text.decode()

        if receiver_name == "this":
            owner = self._find_containing_type(call_node)
            logical = f"{owner}.{method_name}" if owner else None
            if logical and logical in self.top_level_nodes:
                self._add_relationship_raw(
                    caller_id, self.top_level_nodes[logical].id, call_line, True
                )
                return
            if not self._is_call_noise(method_name):
                self._add_relationship_raw(caller_id, method_name, call_line, False)
            return

        if receiver_name[:1].isupper():
            if receiver_name in SCALA_PRIMITIVE_TYPES:
                return
            resolved_id = self._resolve_candidates(
                [f"{receiver_name}$.{method_name}", f"{receiver_name}.{method_name}"]
            )
            if resolved_id:
                self._add_relationship_raw(caller_id, resolved_id, call_line, True)
                return
            if not self._is_call_noise(method_name):
                self._add_relationship_raw(
                    caller_id, f"{receiver_name}.{method_name}", call_line, False
                )
            return

        # Lowercase receiver: try to resolve the variable's declared type.
        var_type = self._find_variable_type(call_node, receiver_name)
        if var_type:
            resolved_id = self._resolve_candidates(
                [f"{var_type}.{method_name}", f"{var_type}$.{method_name}"]
            )
            if resolved_id:
                self._add_relationship_raw(caller_id, resolved_id, call_line, True)
                return
            if not self._is_call_noise(method_name):
                self._add_relationship_raw(caller_id, f"{var_type}.{method_name}", call_line, False)
            return

        if not self._is_call_noise(method_name):
            self._add_relationship_raw(caller_id, method_name, call_line, False)

    def _find_variable_type(self, node, variable_name: str) -> str | None:
        """Best-effort resolution of a local variable's declared type: the
        enclosing method's parameters, a preceding local `val`/`var` in the
        same block, the enclosing class's constructor parameters, or one of
        its fields."""
        func_node = node.parent
        while func_node is not None and func_node.type not in _METHOD_DEFINITION_NODES:
            func_node = func_node.parent

        if func_node is not None:
            # A curried method has one `parameters` node per group; check them all.
            for params_node in func_node.children_by_field_name("parameters"):
                for param in params_node.children:
                    if (
                        param.type == "parameter"
                        and self._field_text(param, "name") == variable_name
                    ):
                        t = self._get_type_name(param.child_by_field_name("type"))
                        if t:
                            return t

            body = func_node.child_by_field_name("body")
            if body is not None and body.type == "block":
                for child in body.children:
                    if child.start_byte >= node.start_byte:
                        break
                    if (
                        child.type
                        in (
                            "val_definition",
                            "var_definition",
                        )
                        and self._field_text(child, "pattern") == variable_name
                    ):
                        t = self._get_type_name(child.child_by_field_name("type"))
                        if t:
                            return t
                        value_node = child.child_by_field_name("value")
                        if value_node is not None and value_node.type == "instance_expression":
                            for c in value_node.children:
                                if c.type in ("type_identifier", "generic_type"):
                                    return self._get_type_name(c)

        class_node = node.parent
        while class_node is not None and class_node.type not in _TYPE_DEFINITION_NODES:
            class_node = class_node.parent

        if class_node is not None:
            class_params = class_node.child_by_field_name("class_parameters")
            if class_params is not None:
                for param in class_params.children:
                    if (
                        param.type == "class_parameter"
                        and self._field_text(param, "name") == variable_name
                    ):
                        t = self._get_type_name(param.child_by_field_name("type"))
                        if t:
                            return t

            body = class_node.child_by_field_name("body")
            if body is not None:
                for child in body.children:
                    if (
                        child.type
                        in (
                            "val_definition",
                            "var_definition",
                        )
                        and self._field_text(child, "pattern") == variable_name
                    ):
                        t = self._get_type_name(child.child_by_field_name("type"))
                        if t:
                            return t

        return None

    # ------------------------------------------------------------------
    # Resolution / noise filtering
    # ------------------------------------------------------------------

    def _resolve_candidates(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            node_obj = self.top_level_nodes.get(candidate)
            if node_obj is not None:
                return node_obj.id
        return None

    def _add_type_relationship(self, caller_id: str, type_name: str, call_line: int):
        resolved_id = self._resolve_candidates([type_name, f"{type_name}$"])
        if resolved_id:
            self._add_relationship_raw(caller_id, resolved_id, call_line, True)
        else:
            self._add_relationship_raw(caller_id, type_name, call_line, False)

    def _add_relationship_raw(
        self, caller: str, callee: str, call_line: int | None, resolved: bool
    ):
        key = (caller, callee, call_line)
        if caller == callee or key in self.seen_relationships:
            return
        self.seen_relationships.add(key)
        self.call_relationships.append(
            CallRelationship(
                caller=caller, callee=callee, call_line=call_line, is_resolved=resolved
            )
        )

    def _is_primitive(self, type_name: str) -> bool:
        return type_name in SCALA_PRIMITIVE_TYPES

    def _is_call_noise(self, name: str) -> bool:
        return name in SCALA_CORE_CALLS


def analyze_scala_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterScalaAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
