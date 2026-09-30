"""Tree-sitter based Rust analyzer.

Extracts structs, unions, enums, traits, free functions, and impl/trait
methods as documentable components, plus trait-impl, field-type,
struct-literal instantiation, and call dependency edges.

Component types follow the same two-level `component_type` / `node_type`
split as the Scala analyzer: `component_type` is the coarse type the
pipeline gates leaf selection on (structs stay "struct", traits map to
"interface", enums to "class"), while `node_type` and `display_name`
preserve the faithful Rust construct.

Methods are keyed by their impl's self type (`Buffer.push`), no matter
how many `impl` blocks contribute them. Owners are joined with `.` rather
than `::` because component ids already use `::` as the path separator.

Known limitations:
- `#[cfg(test)]` modules are skipped entirely, so inline unit tests do not
  flood the graph with test functions.
- tree-sitter does not parse macro token trees, so calls that appear only
  inside macro arguments (`println!("{}", render())`) are not seen.
"""

import logging
import os
from pathlib import Path

import tree_sitter_rust
from tree_sitter import Language, Parser

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node

logger = logging.getLogger(__name__)

# Node types that introduce a component owning fields / methods.
_TYPE_DEFINITION_NODES = ("struct_item", "union_item", "enum_item", "trait_item")
_METHOD_DEFINITION_NODES = ("function_item", "function_signature_item")

# `component_type` / `node_type` per construct.
_TYPE_MAPPING = {
    "struct_item": ("struct", "struct"),
    "union_item": ("struct", "union"),
    "enum_item": ("class", "enum"),
    "trait_item": ("interface", "trait"),
}

# Path roots that always point outside the repository.
_EXTERNAL_PATH_ROOTS = frozenset({"std", "core", "alloc"})
# Path roots that are relative to the current crate/module and carry no
# information for name-based resolution.
_RELATIVE_PATH_ROOTS = frozenset({"crate", "self", "super"})

# Rust primitives and ubiquitous standard-library types excluded from
# dependency edges.
RUST_PRIMITIVE_TYPES = frozenset(
    {
        "i8",
        "i16",
        "i32",
        "i64",
        "i128",
        "isize",
        "u8",
        "u16",
        "u32",
        "u64",
        "u128",
        "usize",
        "f32",
        "f64",
        "bool",
        "char",
        "str",
        "String",
        "Self",
        "Vec",
        "VecDeque",
        "Option",
        "Result",
        "Box",
        "Rc",
        "Arc",
        "Weak",
        "RefCell",
        "Cell",
        "Mutex",
        "RwLock",
        "HashMap",
        "HashSet",
        "BTreeMap",
        "BTreeSet",
        "BinaryHeap",
        "Cow",
        "PhantomData",
        "Pin",
        "Path",
        "PathBuf",
        "Duration",
        "Instant",
        "Error",
    }
)

# Ubiquitous standard-library method and constructor names that never point
# at a repository component. Calls whose only signal is one of these names
# are dropped instead of emitted as unresolved relationships.
RUST_CORE_CALLS = frozenset(
    {
        "Some",
        "Ok",
        "Err",
        "Box",
        "drop",
        "unwrap",
        "unwrap_or",
        "unwrap_or_else",
        "unwrap_or_default",
        "expect",
        "clone",
        "cloned",
        "copied",
        "to_string",
        "to_owned",
        "into",
        "from",
        "try_into",
        "try_from",
        "as_ref",
        "as_mut",
        "as_str",
        "as_slice",
        "borrow",
        "borrow_mut",
        "deref",
        "iter",
        "iter_mut",
        "into_iter",
        "map",
        "map_err",
        "and_then",
        "or_else",
        "ok",
        "ok_or",
        "ok_or_else",
        "filter",
        "filter_map",
        "flat_map",
        "for_each",
        "fold",
        "collect",
        "enumerate",
        "zip",
        "chain",
        "rev",
        "take",
        "skip",
        "any",
        "all",
        "find",
        "position",
        "count",
        "sum",
        "min",
        "max",
        "sort",
        "sort_by",
        "sort_by_key",
        "dedup",
        "push",
        "push_str",
        "pop",
        "insert",
        "remove",
        "get",
        "get_mut",
        "entry",
        "or_insert",
        "or_insert_with",
        "or_default",
        "contains",
        "contains_key",
        "extend",
        "retain",
        "clear",
        "len",
        "is_empty",
        "is_some",
        "is_none",
        "is_ok",
        "is_err",
        "lock",
        "read",
        "write",
        "join",
        "split",
        "trim",
        "starts_with",
        "ends_with",
        "format",
        "fmt",
        "eq",
        "cmp",
        "partial_cmp",
        "hash",
        "default",
        "new",
        "with_capacity",
    }
)


class TreeSitterRustAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        # Same-file symbol table keyed by logical name ("Foo", "Foo.bar").
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
            rust_language = Language(tree_sitter_rust.language())
            parser = Parser(rust_language)
            tree = parser.parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            self._extract_nodes(root, lines)
            self._extract_relationships(root)
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Rust file {self.file_path}: {e}")

    @staticmethod
    def _field_text(node, field_name: str) -> str | None:
        child = node.child_by_field_name(field_name)
        return child.text.decode() if child is not None else None

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _preceding_attributes(node):
        """The `attribute_item`s directly above an item (skipping comments)."""
        attrs = []
        sibling = node.prev_sibling
        while sibling is not None and sibling.type in (
            "attribute_item",
            "line_comment",
            "block_comment",
        ):
            if sibling.type == "attribute_item":
                attrs.append(sibling)
            sibling = sibling.prev_sibling
        return attrs

    def _is_test_module(self, node) -> bool:
        if node.type != "mod_item":
            return False
        for attr in self._preceding_attributes(node):
            text = "".join(attr.text.decode().split())
            if text.startswith("#[cfg(") and "test" in text and "not(test" not in text:
                return True
        return False

    def _module_prefix(self, node) -> str:
        """Dotted path of the inline `mod` blocks enclosing a node."""
        parts = []
        current = node.parent
        while current is not None:
            if current.type == "mod_item":
                name = self._field_text(current, "name")
                if name:
                    parts.append(name)
            current = current.parent
        return ".".join(reversed(parts))

    def _qualify(self, node, name: str) -> str:
        prefix = self._module_prefix(node)
        return f"{prefix}.{name}" if prefix else name

    def _owner_key_for_definition(self, def_node) -> str | None:
        """The logical name a type definition or impl block's self type is
        registered under."""
        if def_node.type == "impl_item":
            name = self._get_type_name(def_node.child_by_field_name("type"))
        else:
            name = self._field_text(def_node, "name")
        if not name:
            return None
        return self._qualify(def_node, name)

    def _find_containing_type(self, node) -> str | None:
        """Walk up to the nearest enclosing impl/trait/struct/enum and return
        its owner key, or None at module scope."""
        current = node.parent
        while current is not None:
            if current.type in _TYPE_DEFINITION_NODES or current.type == "impl_item":
                return self._owner_key_for_definition(current)
            if current.type == "mod_item":
                # A module body is not a member scope of any outer type.
                return None
            current = current.parent
        return None

    def _find_containing_function(self, node):
        current = node.parent
        while current is not None:
            if current.type in _METHOD_DEFINITION_NODES:
                return current
            current = current.parent
        return None

    def _function_logical_name(self, func_node) -> str | None:
        name = self._field_text(func_node, "name")
        if not name:
            return None
        owner = self._find_containing_type(func_node)
        return f"{owner}.{name}" if owner else self._qualify(func_node, name)

    def _resolve_caller(self, node) -> str | None:
        """Prefer the enclosing function; fall back to the enclosing type for
        expressions outside any function body (e.g. const initializers)."""
        func_node = self._find_containing_function(node)
        if func_node is not None:
            logical = self._function_logical_name(func_node)
            if logical and logical in self.top_level_nodes:
                return self.top_level_nodes[logical].id
        owner = self._find_containing_type(node)
        if owner and owner in self.top_level_nodes:
            return self.top_level_nodes[owner].id
        return None

    # ------------------------------------------------------------------
    # Pass 1: components
    # ------------------------------------------------------------------

    def _extract_nodes(self, node, lines):
        if self._is_test_module(node):
            return

        component_type = None
        node_type = None
        logical_name = None
        class_name = None
        parameters = None

        if node.type in _TYPE_DEFINITION_NODES:
            logical_name = self._owner_key_for_definition(node)
            if logical_name:
                component_type, node_type = _TYPE_MAPPING[node.type]
        elif node.type in _METHOD_DEFINITION_NODES:
            name = self._field_text(node, "name")
            if name:
                parameters = self._extract_parameters(node)
                owner = self._find_containing_type(node)
                if owner:
                    component_type, node_type = "method", "method"
                    logical_name = f"{owner}.{name}"
                    class_name = owner
                elif node.type == "function_item":
                    component_type, node_type = "function", "function"
                    logical_name = self._qualify(node, name)

        if component_type and logical_name:
            self._add_node(
                node,
                logical_name,
                component_type,
                node_type,
                lines,
                class_name=class_name,
                parameters=parameters,
            )

        for child in node.children:
            self._extract_nodes(child, lines)

        if node.type == "source_file":
            self._attach_impl_base_classes(node)

    def _attach_impl_base_classes(self, root):
        """Record `impl Trait for Type` as a base class of `Type`."""

        def walk(n):
            if self._is_test_module(n):
                return
            if n.type == "impl_item":
                trait_name = self._get_type_name(n.child_by_field_name("trait"))
                owner = self._owner_key_for_definition(n)
                owner_node = self.top_level_nodes.get(owner) if owner else None
                if trait_name and owner_node is not None:
                    bases = owner_node.base_classes or []
                    if trait_name not in bases:
                        owner_node.base_classes = [*bases, trait_name]
            for child in n.children:
                walk(child)

        walk(root)

    def _extract_parameters(self, func_node) -> list[str] | None:
        params_node = func_node.child_by_field_name("parameters")
        if params_node is None:
            return None
        params = [
            child.text.decode().strip()
            for child in params_node.children
            if child.type in ("parameter", "self_parameter", "variadic_parameter")
        ]
        return params or None

    @staticmethod
    def _is_doc_comment(comment) -> bool:
        text = comment.text.decode()
        return text.startswith(("///", "//!", "/**", "/*!"))

    def _extract_docstring(self, node) -> str:
        comments = []
        sibling = node.prev_sibling
        while sibling is not None and sibling.type in (
            "attribute_item",
            "line_comment",
            "block_comment",
        ):
            if sibling.type != "attribute_item":
                if not self._is_doc_comment(sibling):
                    break
                comments.append(sibling.text.decode().strip())
            sibling = sibling.prev_sibling
        return "\n".join(reversed(comments))

    def _add_node(
        self,
        node,
        logical_name: str,
        component_type: str,
        node_type: str,
        lines,
        class_name: str | None = None,
        parameters: list[str] | None = None,
    ):
        component_id = self._get_component_id(logical_name)
        relative_path = self._get_relative_path()

        docstring = self._extract_docstring(node)

        start_line_idx = node.start_point[0]
        end_line_idx = node.end_point[0] + 1
        code_snippet = (
            "\n".join(lines[start_line_idx:end_line_idx]) if start_line_idx < len(lines) else ""
        )

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
            class_name=class_name,
            display_name=f"{node_type} {logical_name}",
            component_id=component_id,
            language="rust",
        )
        self.nodes.append(node_obj)
        self.top_level_nodes[logical_name] = node_obj

    # ------------------------------------------------------------------
    # Pass 2: relationships
    # ------------------------------------------------------------------

    def _extract_relationships(self, node):
        if self._is_test_module(node):
            return

        if node.type == "impl_item":
            self._emit_impl_edge(node)
        elif node.type in ("field_declaration", "ordered_field_declaration_list"):
            self._emit_field_type_edges(node)
        elif node.type == "struct_expression":
            self._emit_instantiation_edge(node)
        elif node.type == "call_expression":
            self._emit_call_edge(node)

        for child in node.children:
            self._extract_relationships(child)

    def _get_type_name(self, node) -> str | None:
        """Get the primary type name from a type node, stripping generics,
        references, pointers, and module paths."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return node.text.decode()
        if node.type in ("generic_type", "reference_type", "pointer_type"):
            return self._get_type_name(node.child_by_field_name("type"))
        if node.type == "scoped_type_identifier":
            path = node.child_by_field_name("path")
            if path is not None and path.text.decode().split("::")[0] in _EXTERNAL_PATH_ROOTS:
                return None
            return self._field_text(node, "name")
        return None

    def _emit_impl_edge(self, impl_node):
        trait_name = self._get_type_name(impl_node.child_by_field_name("trait"))
        owner = self._owner_key_for_definition(impl_node)
        if not trait_name or not owner or owner not in self.top_level_nodes:
            return
        if self._is_primitive(trait_name):
            return
        caller_id = self.top_level_nodes[owner].id
        self._add_type_relationship(caller_id, trait_name, impl_node.start_point[0] + 1)

    def _field_owner(self, node) -> str | None:
        current = node.parent
        while current is not None:
            if current.type in _TYPE_DEFINITION_NODES:
                return self._owner_key_for_definition(current)
            current = current.parent
        return None

    def _emit_field_type_edges(self, node):
        owner = self._field_owner(node)
        if not owner or owner not in self.top_level_nodes:
            return
        caller_id = self.top_level_nodes[owner].id
        if node.type == "field_declaration":
            type_nodes = [node.child_by_field_name("type")]
        else:
            type_nodes = node.children_by_field_name("type")
        for type_node in type_nodes:
            for type_name in self._collect_type_names(type_node):
                self._add_type_relationship(caller_id, type_name, node.start_point[0] + 1)

    def _collect_type_names(self, type_node) -> list[str]:
        """The primary type plus any project-looking generic arguments, so
        `Vec<Logger>` and `Option<Box<Store>>` still yield an edge."""
        names: list[str] = []
        if type_node is None:
            return names
        generics = self._type_params_in_scope(type_node)

        def walk(n):
            if n is None:
                return
            name = self._get_type_name(n)
            if name and not self._is_primitive(name) and name not in generics and name not in names:
                names.append(name)
            if n.type in ("generic_type", "reference_type", "pointer_type"):
                inner = n.child_by_field_name("type")
                if n.type != "generic_type":
                    walk(inner)
                args = n.child_by_field_name("type_arguments")
                if args is not None:
                    for arg in args.children:
                        walk(arg)
            elif n.type in ("tuple_type", "array_type"):
                for child in n.children:
                    walk(child)

        walk(type_node)
        return names

    @staticmethod
    def _type_params_in_scope(node) -> set[str]:
        """Generic parameter names declared by the items enclosing a node."""
        params: set[str] = set()
        current = node
        while current is not None:
            type_params = current.child_by_field_name("type_parameters")
            if type_params is not None:
                for param in type_params.named_children:
                    name = param.child_by_field_name("name")
                    if name is None and param.type == "type_identifier":
                        name = param
                    if name is not None:
                        params.add(name.text.decode())
            current = current.parent
        return params

    def _emit_instantiation_edge(self, struct_node):
        caller_id = self._resolve_caller(struct_node)
        if not caller_id:
            return
        name_node = struct_node.child_by_field_name("name")
        type_name = self._get_type_name(name_node)
        if name_node is not None and name_node.type == "scoped_identifier":
            type_name = self._field_text(name_node, "name")
        if type_name == "Self":
            type_name = self._find_containing_type(struct_node)
        if type_name and not self._is_primitive(type_name):
            self._add_type_relationship(caller_id, type_name, struct_node.start_point[0] + 1)

    def _emit_call_edge(self, call_node):
        caller_id = self._resolve_caller(call_node)
        if not caller_id:
            return
        func_node = call_node.child_by_field_name("function")
        if func_node is not None and func_node.type == "generic_function":
            func_node = func_node.child_by_field_name("function")
        if func_node is None:
            return
        call_line = call_node.start_point[0] + 1

        if func_node.type == "identifier":
            self._emit_bare_call_edge(caller_id, func_node.text.decode(), call_node, call_line)
        elif func_node.type == "scoped_identifier":
            self._emit_path_call_edge(caller_id, func_node, call_node, call_line)
        elif func_node.type == "field_expression":
            self._emit_method_call_edge(caller_id, func_node, call_node, call_line)

    def _emit_bare_call_edge(self, caller_id: str, callee_name: str, call_node, call_line: int):
        candidates = []
        owner = self._find_containing_type(call_node)
        if owner:
            candidates.append(f"{owner}.{callee_name}")
        candidates.append(self._qualify(call_node, callee_name))
        candidates.append(callee_name)

        resolved_id = self._resolve_candidates(candidates)
        if resolved_id:
            self._add_relationship_raw(caller_id, resolved_id, call_line, True)
        elif not self._is_call_noise(callee_name):
            self._add_relationship_raw(caller_id, callee_name, call_line, False)

    @staticmethod
    def _path_segments(scoped_node) -> list[str]:
        return [seg for seg in scoped_node.text.decode().replace(" ", "").split("::") if seg]

    def _emit_path_call_edge(self, caller_id: str, func_node, call_node, call_line: int):
        segments = self._path_segments(func_node)
        # Drop turbofish generics (`Vec::<T>::new`) left in the path text.
        segments = [s.split("<")[0] for s in segments if not s.startswith("<")]
        segments = [s for s in segments if s]
        if not segments or segments[0] in _EXTERNAL_PATH_ROOTS:
            return
        while segments and segments[0] in _RELATIVE_PATH_ROOTS:
            segments = segments[1:]
        if not segments:
            return

        name = segments[-1]
        if len(segments) >= 2 and segments[-2] == "Self":
            owner = self._find_containing_type(call_node)
            if not owner:
                return
            qualifier = owner
        elif len(segments) >= 2 and segments[-2][:1].isupper():
            qualifier = segments[-2]
            if self._is_primitive(qualifier):
                return
        else:
            # A module-qualified free function (`util::helper`): the module
            # path is carried by the file, so only the function name remains.
            qualifier = None

        if qualifier:
            logical = f"{qualifier}.{name}"
            resolved_id = self._resolve_candidates([logical, self._qualify(call_node, logical)])
            if resolved_id:
                self._add_relationship_raw(caller_id, resolved_id, call_line, True)
            else:
                # Keep constructor-shaped calls (`Store::new`) even though the
                # bare method name is noise: the qualifier is the signal.
                self._add_relationship_raw(caller_id, logical, call_line, False)
            return

        resolved_id = self._resolve_candidates([self._qualify(call_node, name), name])
        if resolved_id:
            self._add_relationship_raw(caller_id, resolved_id, call_line, True)
        elif not self._is_call_noise(name):
            self._add_relationship_raw(caller_id, name, call_line, False)

    def _emit_method_call_edge(self, caller_id: str, func_node, call_node, call_line: int):
        receiver = func_node.child_by_field_name("value")
        method_node = func_node.child_by_field_name("field")
        if receiver is None or method_node is None:
            return
        method_name = method_node.text.decode()

        if receiver.type == "self":
            owner = self._find_containing_type(call_node)
            resolved_id = self._resolve_candidates([f"{owner}.{method_name}"]) if owner else None
            if resolved_id:
                self._add_relationship_raw(caller_id, resolved_id, call_line, True)
            elif not self._is_call_noise(method_name):
                self._add_relationship_raw(caller_id, method_name, call_line, False)
            return

        var_type = None
        if receiver.type == "identifier":
            var_type = self._find_variable_type(call_node, receiver.text.decode())
        elif receiver.type == "field_expression":
            var_type = self._find_self_field_type(call_node, receiver)

        if var_type:
            logical = f"{var_type}.{method_name}"
            resolved_id = self._resolve_candidates([logical])
            if resolved_id:
                self._add_relationship_raw(caller_id, resolved_id, call_line, True)
            elif not self._is_call_noise(method_name):
                self._add_relationship_raw(caller_id, logical, call_line, False)
            return

        if not self._is_call_noise(method_name):
            self._add_relationship_raw(caller_id, method_name, call_line, False)

    def _find_self_field_type(self, node, field_expr) -> str | None:
        """Type of `self.field` from the owning struct's declaration."""
        value = field_expr.child_by_field_name("value")
        field = field_expr.child_by_field_name("field")
        if value is None or value.type != "self" or field is None:
            return None
        owner = self._find_containing_type(node)
        owner_node = self.top_level_nodes.get(owner) if owner else None
        if owner_node is None:
            return None
        field_name = field.text.decode()
        root = node
        while root.parent is not None:
            root = root.parent
        return self._struct_field_type(root, owner, field_name)

    def _struct_field_type(self, root, owner: str, field_name: str) -> str | None:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "struct_item" and self._owner_key_for_definition(n) == owner:
                body = n.child_by_field_name("body")
                if body is None:
                    return None
                for decl in body.children:
                    if (
                        decl.type == "field_declaration"
                        and self._field_text(decl, "name") == field_name
                    ):
                        names = self._collect_type_names(decl.child_by_field_name("type"))
                        return names[0] if names else None
                return None
            stack.extend(n.children)
        return None

    def _find_variable_type(self, node, variable_name: str) -> str | None:
        """Best-effort resolution of a local's type: the enclosing function's
        parameters, or a preceding `let` in an enclosing block with a type
        annotation, a `Type::ctor(..)` call, or a `Type { .. }` literal."""
        func_node = self._find_containing_function(node)
        if func_node is None:
            return None

        params_node = func_node.child_by_field_name("parameters")
        if params_node is not None:
            for param in params_node.children:
                if (
                    param.type == "parameter"
                    and self._field_text(param, "pattern") == variable_name
                ):
                    names = self._collect_type_names(param.child_by_field_name("type"))
                    if names:
                        return names[0]

        block = node.parent
        while block is not None and block is not func_node:
            if block.type == "block":
                for child in block.children:
                    if child.start_byte >= node.start_byte:
                        break
                    if (
                        child.type == "let_declaration"
                        and self._field_text(child, "pattern") == variable_name
                    ):
                        found = self._let_binding_type(child, node)
                        if found:
                            return found
            block = block.parent
        return None

    def _let_binding_type(self, let_node, use_node) -> str | None:
        names = self._collect_type_names(let_node.child_by_field_name("type"))
        if names:
            return names[0]
        value = let_node.child_by_field_name("value")
        # Peel `?` / `.await` / `.unwrap()`-free wrappers around the initializer.
        while value is not None and value.type in ("try_expression", "await_expression"):
            value = value.named_children[0] if value.named_children else None
        if value is None:
            return None
        if value.type == "struct_expression":
            name = self._get_type_name(value.child_by_field_name("name"))
            if name == "Self":
                return self._find_containing_type(use_node)
            return name if name and not self._is_primitive(name) else None
        if value.type == "call_expression":
            func = value.child_by_field_name("function")
            if func is not None and func.type == "scoped_identifier":
                segments = [s for s in self._path_segments(func) if s]
                if len(segments) >= 2:
                    qualifier = segments[-2].split("<")[0]
                    if qualifier == "Self":
                        return self._find_containing_type(use_node)
                    if qualifier[:1].isupper() and not self._is_primitive(qualifier):
                        return qualifier
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
        resolved_id = self._resolve_candidates([type_name])
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
        return type_name in RUST_PRIMITIVE_TYPES

    def _is_call_noise(self, name: str) -> bool:
        return name in RUST_CORE_CALLS


def analyze_rust_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterRustAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
