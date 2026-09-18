"""Tree-sitter based C# dependency analyzer.

Ported from the Java analyzer (``java.py``) so C# reaches the same standard as
the other improved parsers: methods become first-class components, names are
namespace-qualified, ``using`` directives drive symbol resolution and external
filtering, and every cross-reference is emitted unresolved for the shared
cross-file resolver to match.

Known limitations (deliberately punted):
  * Partial classes declared across several files produce one node per file
    sharing a qualified name; a bare reference stays an honest unresolved gap
    (the resolver never binds it to an arbitrary half).
  * Top-level statements (``global_statement``) have no containing type, so
    calls made there are not attributed.
  * Cross-file ``global using`` visibility, ``new()`` implicit object creation,
    tuple types, and chained-receiver typing (``a.B().C()``) are not modelled.
"""

import logging
from typing import List, Optional, Tuple
from pathlib import Path
import os

from tree_sitter import Parser, Language
import tree_sitter_c_sharp
from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    CSHARP_OBJECT_METHODS,
)

logger = logging.getLogger(__name__)

# Node types that denote a type reference and can be unwrapped to a name.
_TYPE_NODES = {
    "identifier",
    "qualified_name",
    "generic_name",
    "predefined_type",
    "nullable_type",
    "array_type",
    "pointer_type",
}

# C# type declarations that introduce a named scope.
_TYPE_DECLS = {
    "class_declaration",
    "interface_declaration",
    "struct_declaration",
    "enum_declaration",
    "record_declaration",
}

# C# primitive / contextual keyword types — never project components.
_CSHARP_PRIMITIVES = {
    "bool",
    "byte",
    "sbyte",
    "char",
    "decimal",
    "double",
    "float",
    "int",
    "uint",
    "nint",
    "nuint",
    "long",
    "ulong",
    "short",
    "ushort",
    "string",
    "object",
    "void",
    "var",
    "dynamic",
}


class TreeSitterCSharpAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []
        self.alias_map: dict[str, str] = {}
        self.using_namespaces: list[str] = []
        self.static_usings: list[str] = []
        self.file_scoped_namespace: str = ""
        self._analyze()

    def _get_relative_path(self) -> str:
        if self.repo_path:
            try:
                return os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                return str(self.file_path)
        return str(self.file_path)

    def _get_component_id(self, name: str) -> str:
        return f"{self._get_relative_path()}::{name}"

    def _analyze(self):
        language_capsule = tree_sitter_c_sharp.language()
        cs_language = Language(language_capsule)
        parser = Parser(cs_language)
        tree = parser.parse(bytes(self.content, "utf8"))
        root = tree.root_node
        lines = self.content.splitlines()

        self._extract_usings(root)

        top_level_nodes = {}
        self._extract_nodes(root, top_level_nodes, lines)
        self._extract_relationships(root, top_level_nodes)

    # ------------------------------------------------------------------ usings

    def _extract_usings(self, node):
        """Collect ``using`` directives (plain / alias / static) and the
        file-scoped namespace via an AST walk — directives can sit inside block
        namespaces and come in several forms that a regex would miss."""
        if node.type == "file_scoped_namespace_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                self.file_scoped_namespace = name_node.text.decode()
        elif node.type == "using_directive":
            children = [c for c in node.children if c.type not in ("using", ";", "global")]
            has_static = any(c.type == "static" for c in node.children)
            has_alias = any(c.type == "=" for c in node.children)
            name_children = [c for c in children if c.type in ("identifier", "qualified_name")]
            if has_alias and len(name_children) >= 2:
                self.alias_map[name_children[0].text.decode()] = name_children[1].text.decode()
            elif has_static and name_children:
                self.static_usings.append(name_children[-1].text.decode())
            elif name_children:
                self.using_namespaces.append(name_children[-1].text.decode())

        for child in node.children:
            self._extract_usings(child)

    # ------------------------------------------------------------- namespaces

    def _namespace_for(self, node) -> str:
        """The namespace in scope at ``node``: the file-scoped namespace (if
        any) followed by every enclosing block ``namespace_declaration``."""
        parts = []
        current = node
        while current:
            if current.type == "namespace_declaration":
                name_node = current.child_by_field_name("name")
                if name_node:
                    parts.append(name_node.text.decode())
            current = current.parent
        parts.reverse()
        if self.file_scoped_namespace:
            parts.insert(0, self.file_scoped_namespace)
        return ".".join(p for p in parts if p)

    def _qualify(self, node, *names) -> str:
        ns = self._namespace_for(node)
        parts = [ns] if ns else []
        parts.extend(n for n in names if n)
        return ".".join(parts)

    # ------------------------------------------------------------------ nodes

    def _extract_nodes(self, node, top_level_nodes, lines):
        node_type = None
        node_name = None
        qualified_name = None
        class_name = None

        if node.type == "class_declaration":
            is_abstract = any(
                c.type == "modifier" and c.text.decode() == "abstract" for c in node.children
            )
            is_static = any(
                c.type == "modifier" and c.text.decode() == "static" for c in node.children
            )
            node_type = (
                "static class" if is_static else ("abstract class" if is_abstract else "class")
            )
            node_name = self._decl_name(node)
            qualified_name = self._qualify(node, *self._find_containing_type_names(node), node_name)
        elif node.type == "interface_declaration":
            node_type = "interface"
            node_name = self._decl_name(node)
            qualified_name = self._qualify(node, *self._find_containing_type_names(node), node_name)
        elif node.type == "struct_declaration":
            node_type = "struct"
            node_name = self._decl_name(node)
            qualified_name = self._qualify(node, *self._find_containing_type_names(node), node_name)
        elif node.type == "enum_declaration":
            node_type = "enum"
            node_name = self._decl_name(node)
            qualified_name = self._qualify(node, *self._find_containing_type_names(node), node_name)
        elif node.type == "record_declaration":
            is_struct = any(c.type == "struct" for c in node.children)
            node_type = "record struct" if is_struct else "record"
            node_name = self._decl_name(node)
            qualified_name = self._qualify(node, *self._find_containing_type_names(node), node_name)
        elif node.type == "delegate_declaration":
            node_type = "delegate"
            node_name = self._decl_name(node)
            qualified_name = self._qualify(node, *self._find_containing_type_names(node), node_name)
        elif node.type == "method_declaration":
            method_name = self._decl_name(node)
            if method_name:
                node_type = "method"
                containing_types = self._find_containing_type_names(node)
                if containing_types:
                    class_name = containing_types[-1]
                    node_name = f"{class_name}.{method_name}"
                    qualified_name = self._qualify(node, *containing_types, method_name)
                else:
                    node_name = method_name
                    qualified_name = self._qualify(node, method_name)

        if node_type and node_name:
            component_id = self._get_component_id(node_name)
            has_docstring, docstring = self._extract_doc_comment(node)
            node_obj = Node(
                id=component_id,
                name=node_name,
                component_type=node_type,
                file_path=str(self.file_path),
                relative_path=self._get_relative_path(),
                source_code="\n".join(lines[node.start_point[0] : node.end_point[0] + 1]),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                has_docstring=has_docstring,
                docstring=docstring,
                parameters=None,
                node_type=node_type,
                base_classes=None,
                class_name=class_name,
                display_name=f"{node_type} {node_name}",
                component_id=component_id,
                language="csharp",
                qualified_name=qualified_name,
            )
            self.nodes.append(node_obj)
            top_level_nodes[node_name] = node_obj
            top_level_nodes[component_id] = node_obj
            if qualified_name:
                top_level_nodes[qualified_name] = node_obj
                top_level_nodes.setdefault(qualified_name.split(".")[-1], node_obj)

        for child in node.children:
            self._extract_nodes(child, top_level_nodes, lines)

    def _extract_doc_comment(self, node) -> Tuple[bool, str]:
        """Collect a ``///`` XML doc-comment block immediately preceding the
        declaration (skipping any attribute lists between comment and decl)."""
        comments = []
        sib = node.prev_sibling
        while sib is not None:
            if sib.type == "attribute_list":
                sib = sib.prev_sibling
                continue
            if sib.type == "comment" and sib.text.decode().lstrip().startswith("///"):
                comments.append(sib.text.decode())
                sib = sib.prev_sibling
                continue
            break
        if not comments:
            return False, ""
        comments.reverse()
        cleaned = []
        for c in comments:
            stripped = c.strip()
            if stripped.startswith("///"):
                stripped = stripped[3:].strip()
            cleaned.append(stripped)
        return True, "\n".join(cleaned)

    # ---------------------------------------------------------- relationships

    def _extract_relationships(self, node, top_level_nodes):
        # 1. Base list — extends and/or implements (C# does not distinguish).
        if node.type in _TYPE_DECLS:
            decl_name = self._decl_name(node)
            base_list = next((c for c in node.children if c.type == "base_list"), None)
            if decl_name and base_list:
                caller_id = self._get_component_id(decl_name)
                for base_type in self._base_list_types(base_list):
                    if not self._skip_type(base_type, node):
                        self.call_relationships.append(
                            CallRelationship(
                                caller=caller_id,
                                callee=self._resolve_cs_type(base_type, node, top_level_nodes),
                                call_line=node.start_point[0] + 1,
                                is_resolved=False,
                            )
                        )

        # 2. Field / property / event type use + primary-constructor params.
        if node.type == "field_declaration":
            self._emit_type_use(self._variable_declaration_type(node), node, top_level_nodes)
        elif node.type == "event_field_declaration":
            self._emit_type_use(self._variable_declaration_type(node), node, top_level_nodes)
        elif node.type == "property_declaration":
            self._emit_type_use(node.child_by_field_name("type"), node, top_level_nodes)
        elif node.type in ("class_declaration", "struct_declaration", "record_declaration"):
            # Primary constructor parameters (C# 12 / records).
            param_list = next((c for c in node.children if c.type == "parameter_list"), None)
            if param_list:
                for param in param_list.children:
                    if param.type == "parameter":
                        self._emit_type_use(
                            param.child_by_field_name("type"), node, top_level_nodes
                        )

        # 3. Method / function invocations.
        if node.type == "invocation_expression":
            self._handle_invocation(node, top_level_nodes)

        # 4. Object creation.
        if node.type == "object_creation_expression":
            containing_class = self._find_containing_class(node, top_level_nodes)
            type_node = node.child_by_field_name("type")
            if containing_class and type_node:
                created_type = self._unwrap_type(type_node)
                if created_type and not self._skip_type(created_type, node):
                    self.call_relationships.append(
                        CallRelationship(
                            caller=containing_class,
                            callee=self._resolve_cs_type(created_type, node, top_level_nodes),
                            call_line=node.start_point[0] + 1,
                            is_resolved=False,
                        )
                    )

        for child in node.children:
            self._extract_relationships(child, top_level_nodes)

    def _emit_type_use(self, type_node, context_node, top_level_nodes):
        if type_node is None:
            return
        containing_class = self._find_containing_class(context_node, top_level_nodes)
        if not containing_class:
            return
        type_name = self._unwrap_type(type_node)
        if type_name and not self._skip_type(type_name, context_node):
            self.call_relationships.append(
                CallRelationship(
                    caller=containing_class,
                    callee=self._resolve_cs_type(type_name, context_node, top_level_nodes),
                    call_line=context_node.start_point[0] + 1,
                    is_resolved=False,
                )
            )

    def _handle_invocation(self, node, top_level_nodes):
        containing_class = self._find_containing_class(node, top_level_nodes)
        if not containing_class:
            return
        caller_id = self._find_containing_method(node) or containing_class

        func = node.child_by_field_name("function")
        if func is None:
            return

        method_name = None
        target_type = None
        bare = False

        if func.type == "identifier":
            method_name = func.text.decode()
            bare = True
        elif func.type == "member_access_expression":
            name_node = func.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                return
            method_name = name_node.text.decode()
            expr = func.child_by_field_name("expression")
            if expr is None:
                return
            if expr.type == "this_expression":
                bare = True
            elif expr.type == "base_expression":
                target_type = self._first_base_type(node)
                if target_type is None:
                    bare = True
            elif expr.type == "identifier":
                receiver = expr.text.decode()
                target_type = self._find_variable_type(node, receiver)
                if not target_type and receiver in top_level_nodes:
                    target_type = receiver
                if not target_type and receiver[:1].isupper() and not receiver.isupper():
                    # PascalCase receiver with no matching variable reads as a
                    # static call on a type from another file or a using; an
                    # ALL_CAPS receiver is a constant, not a type.
                    target_type = receiver
            else:
                # Chained / qualified receiver (a.B().C(), Outer.Inner.M()).
                return
        else:
            return

        if method_name in (None, "nameof"):
            return

        if bare:
            # A local / inherited member of an enclosing type (same-file nodes
            # match here; cross-file ones resolve later by simple/tail name).
            for candidate in self._enclosing_member_candidates(node, method_name):
                if candidate in top_level_nodes:
                    self._add_edge(caller_id, candidate, node)
                    return
            # Otherwise it may be a statically-imported member. We cannot know
            # which `using static` it came from, so emit one candidate per static
            # class: the right one resolves cross-file, framework guesses
            # (System.*) are filtered downstream, and a member that exists in no
            # static class simply stays an honest unresolved gap.
            for static_using in self.static_usings:
                self._add_edge(caller_id, f"{static_using}.{method_name}", node)
            return

        if target_type and not self._skip_type(target_type, node):
            callee = self._resolve_cs_member(method_name, node, top_level_nodes, target_type)
            if callee not in top_level_nodes and method_name in CSHARP_OBJECT_METHODS:
                # Inherited System.Object method the project type does not
                # override locally — never a project edge.
                return
            self._add_edge(caller_id, callee, node)

    def _add_edge(self, caller, callee, node):
        self.call_relationships.append(
            CallRelationship(
                caller=caller,
                callee=callee,
                call_line=node.start_point[0] + 1,
                is_resolved=False,
            )
        )

    def _enclosing_member_candidates(self, node, member_name):
        containing_types = self._find_containing_type_names(node)
        return [
            self._qualify(node, *containing_types[:idx], member_name)
            for idx in range(len(containing_types), 0, -1)
        ]

    # ------------------------------------------------------------ type helpers

    def _base_list_types(self, base_list):
        types = []
        for child in base_list.children:
            if child.type in ("identifier", "qualified_name", "generic_name"):
                name = self._unwrap_type(child)
                if name:
                    types.append(name)
            elif child.type == "primary_constructor_base_type":
                inner = next(
                    (
                        c
                        for c in child.children
                        if c.type in ("identifier", "qualified_name", "generic_name")
                    ),
                    None,
                )
                name = self._unwrap_type(inner) if inner else None
                if name:
                    types.append(name)
        return types

    def _first_base_type(self, node):
        """First base type of the type enclosing ``node`` (for ``base.X()``)."""
        current = node.parent
        while current:
            if current.type in _TYPE_DECLS:
                base_list = next((c for c in current.children if c.type == "base_list"), None)
                if base_list:
                    bases = self._base_list_types(base_list)
                    return bases[0] if bases else None
                return None
            current = current.parent
        return None

    def _variable_declaration_type(self, node):
        vd = next((c for c in node.children if c.type == "variable_declaration"), None)
        if vd is None:
            return None
        return self._first_type_child(vd)

    def _first_type_child(self, node):
        return next(
            (c for c in node.children if c.type in _TYPE_NODES or c.type == "implicit_type"),
            None,
        )

    def _unwrap_type(self, node) -> Optional[str]:
        if node is None:
            return None
        if node.type in ("identifier", "qualified_name", "predefined_type"):
            return node.text.decode()
        if node.type == "generic_name":
            ident = next((c for c in node.children if c.type == "identifier"), None)
            return ident.text.decode() if ident else None
        if node.type in ("nullable_type", "array_type", "pointer_type"):
            inner = node.child_by_field_name("type")
            if inner is None:
                inner = next((c for c in node.children if c.type in _TYPE_NODES), None)
            return self._unwrap_type(inner)
        return None

    def _simple_type_name(self, type_name: str) -> str:
        return type_name.strip().split("<", 1)[0].strip()

    def _skip_type(self, type_name: str, context_node) -> bool:
        """Types that can *never* be a project component: language primitives and
        generic type parameters in scope.

        Framework types (``List``, ``Console``, ``Task``...) are deliberately NOT
        filtered here. Doing so at extraction time would drop a real edge whenever
        a project type shadows a framework name (`resolve first, filter second`,
        per the parser audit). Their references are emitted unresolved and the
        cross-file resolver gets first chance; only the still-unresolved ones are
        classified external downstream in ``_is_external_callee``. Alias targets
        are likewise left to ``_resolve_cs_type``, which expands the alias to its
        fully-qualified form so a `using S = System.X;` reference filters
        downstream by its real namespace."""
        if not type_name:
            return True
        simple = self._simple_type_name(type_name)
        if simple in _CSHARP_PRIMITIVES:
            return True
        return simple in self._find_type_parameters(context_node)

    def _find_type_parameters(self, node) -> set:
        params = set()
        current = node
        while current:
            if current.type in (
                *_TYPE_DECLS,
                "delegate_declaration",
                "method_declaration",
                "local_function_statement",
            ):
                type_params = next(
                    (c for c in current.children if c.type == "type_parameter_list"), None
                )
                if type_params:
                    for param in type_params.children:
                        if param.type == "type_parameter":
                            ident = next(
                                (c for c in param.children if c.type == "identifier"), None
                            )
                            if ident:
                                params.add(ident.text.decode())
            current = current.parent
        return params

    def _resolve_cs_type(self, type_name: str, context_node, top_level_nodes) -> str:
        if not type_name:
            return type_name
        simple = self._simple_type_name(type_name)
        if "." in simple:
            return simple
        if simple in self.alias_map:
            return self.alias_map[simple]
        containing_types = self._find_containing_type_names(context_node)
        for idx in range(len(containing_types), 0, -1):
            candidate = self._qualify(context_node, *containing_types[:idx], simple)
            if candidate in top_level_nodes:
                return candidate
        # A `using MyApp.Models;` makes a type in that namespace reachable by its
        # simple name; prefer a known project node there over a bare guess.
        for namespace in self.using_namespaces:
            candidate = f"{namespace}.{simple}"
            if candidate in top_level_nodes:
                return candidate
        # Unlike Java, C# has no per-type imports, so the real namespace of an
        # unresolved simple type is unknown. Returning the bare name (rather than
        # fabricating the file's own namespace) lets a genuine project type still
        # match cross-file by its simple/tail name, while a third-party type used
        # via `using SomeLib;` stays unqualified — so the resolver's
        # namespace-origin rule can classify `SomeLib.Type.method`-style edges as
        # external instead of mistaking the fabricated namespace for the project.
        return simple

    def _resolve_cs_member(self, member_name, context_node, top_level_nodes, target_type) -> str:
        """Resolve a member call on a known receiver type to a `Type.member`
        candidate the cross-file resolver can match."""
        qualified_type = self._resolve_cs_type(target_type, context_node, top_level_nodes)
        candidate = f"{qualified_type}.{member_name}"
        if candidate in top_level_nodes:
            return candidate
        simple_candidate = f"{qualified_type.split('.')[-1]}.{member_name}"
        if simple_candidate in top_level_nodes:
            return simple_candidate
        return candidate

    # ----------------------------------------------------------- scope lookup

    def _decl_name(self, node) -> Optional[str]:
        name_node = node.child_by_field_name("name")
        return name_node.text.decode() if name_node else None

    def _find_containing_class(self, node, top_level_nodes):
        current = node.parent
        while current:
            if current.type in _TYPE_DECLS:
                class_name = self._decl_name(current)
                if class_name and class_name in top_level_nodes:
                    return self._get_component_id(class_name)
            current = current.parent
        return None

    def _find_containing_class_name(self, node):
        names = self._find_containing_type_names(node)
        return names[-1] if names else None

    def _find_containing_type_names(self, node) -> list:
        names = []
        current = node.parent
        while current:
            if current.type in _TYPE_DECLS:
                name = self._decl_name(current)
                if name:
                    names.append(name)
            current = current.parent
        return list(reversed(names))

    def _find_containing_method(self, node):
        current = node.parent
        while current:
            if current.type == "method_declaration":
                method_name = self._decl_name(current)
                class_name = self._find_containing_class_name(current)
                if method_name and class_name:
                    return self._get_component_id(f"{class_name}.{method_name}")
            current = current.parent
        return None

    def _find_variable_type(self, node, variable_name):
        # Method / constructor / local-function scope: parameters and locals.
        method_node = node.parent
        while method_node and method_node.type not in (
            "method_declaration",
            "constructor_declaration",
            "local_function_statement",
        ):
            method_node = method_node.parent

        if method_node:
            param_list = next((c for c in method_node.children if c.type == "parameter_list"), None)
            param_type = self._param_type(param_list, variable_name)
            if param_type:
                return param_type
            body = next((c for c in method_node.children if c.type == "block"), None)
            if body:
                local_type = self._search_variable_declaration(body, variable_name)
                if local_type:
                    return local_type

        # Class scope: primary-constructor params, fields, and properties.
        class_node = node.parent
        while class_node and class_node.type not in _TYPE_DECLS:
            class_node = class_node.parent

        if class_node:
            param_list = next((c for c in class_node.children if c.type == "parameter_list"), None)
            param_type = self._param_type(param_list, variable_name)
            if param_type:
                return param_type
            body = next((c for c in class_node.children if c.type == "declaration_list"), None)
            if body:
                for member in body.children:
                    if member.type in ("field_declaration", "event_field_declaration"):
                        vd = next(
                            (c for c in member.children if c.type == "variable_declaration"), None
                        )
                        if vd and self._declares_variable(vd, variable_name):
                            return self._unwrap_type(self._first_type_child(vd))
                    elif member.type == "property_declaration":
                        name_node = member.child_by_field_name("name")
                        if name_node and name_node.text.decode() == variable_name:
                            return self._unwrap_type(member.child_by_field_name("type"))
        return None

    def _param_type(self, param_list, variable_name):
        if param_list is None:
            return None
        for param in param_list.children:
            if param.type == "parameter":
                name_node = param.child_by_field_name("name")
                type_node = param.child_by_field_name("type")
                if name_node and type_node and name_node.text.decode() == variable_name:
                    return self._unwrap_type(type_node)
        return None

    def _declares_variable(self, variable_declaration, variable_name) -> bool:
        for child in variable_declaration.children:
            if child.type == "variable_declarator":
                ident = next((c for c in child.children if c.type == "identifier"), None)
                if ident and ident.text.decode() == variable_name:
                    return True
        return False

    def _search_variable_declaration(self, block_node, variable_name):
        for child in block_node.children:
            if child.type == "local_declaration_statement":
                vd = next((c for c in child.children if c.type == "variable_declaration"), None)
                if vd is None:
                    continue
                type_node = self._first_type_child(vd)
                for decl in vd.children:
                    if decl.type != "variable_declarator":
                        continue
                    ident = next((c for c in decl.children if c.type == "identifier"), None)
                    if not (ident and ident.text.decode() == variable_name):
                        continue
                    if type_node is not None and type_node.type != "implicit_type":
                        return self._unwrap_type(type_node)
                    # `var` — recover the type only from a `new T()` initializer.
                    init = next(
                        (c for c in decl.children if c.type == "object_creation_expression"), None
                    )
                    if init is not None:
                        return self._unwrap_type(init.child_by_field_name("type"))
                    return None
            elif child.type in (
                "block",
                "if_statement",
                "else_clause",
                "for_statement",
                "foreach_statement",
                "while_statement",
                "do_statement",
                "using_statement",
                "try_statement",
                "catch_clause",
                "finally_clause",
                "switch_statement",
                "lock_statement",
                "switch_section",
            ):
                result = self._search_variable_declaration(child, variable_name)
                if result:
                    return result
        return None


def analyze_csharp_file(
    file_path: str, content: str, repo_path: str = None
) -> Tuple[List[Node], List[CallRelationship]]:
    analyzer = TreeSitterCSharpAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
