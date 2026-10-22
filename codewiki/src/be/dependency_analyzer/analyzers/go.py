"""Tree-sitter based Go analyzer.

Extracts functions, methods, structs, interfaces (with their method
signatures), and other defined types (`type Kind int`) as documentable
components, plus embedding, field-type, composite-literal instantiation,
call, and interface-implementation dependency edges.

Component types follow the same two-level `component_type` / `node_type`
split as the Rust analyzer: `component_type` is the coarse type the
pipeline gates leaf selection on (structs stay "struct", interfaces
"interface", other defined types map to "class"), while `node_type` and
`display_name` preserve the faithful Go construct.

Methods are keyed by their receiver's base type (`Buffer.Push` for both
`func (b *Buffer) Push` and `func (b Buffer) Push`), no matter which file
of the package declares them. Owners are joined with `.` rather than `::`
because component ids already use `::` as the path separator.

A Go package is a directory, so a name that is not defined in the file
itself is emitted as a *package reference*, `"<dir>/*.go::<Name>"` (the
repository root is `"*.go::<Name>"`): "the declaration `Name` in some file
of the package in `<dir>`". Imports of the repository's own packages are
mapped to directories through the module path in `go.mod` (and local
`replace` directives); calls into the standard library or third-party
modules are dropped. `CallGraphAnalyzer` resolves package references
against the components of that directory (`resolve_go_callee`) and links
types to the interfaces of their package that they implement
(`link_go_interfaces`).

Known limitations:
- `_test.go` files and anything under `vendor/` produce no components, the
  way the Rust analyzer skips `#[cfg(test)]` modules.
- Local variable types are inferred from declarations, composite literals,
  `new(T)`, and same-file function results only; a method call on a value
  of unknown type falls back to its method name, which resolves only when
  exactly one method of that name exists in the caller's package or the
  repository packages it imports.
- Methods promoted through embedding resolve for embedded types of the
  same package only; dot imports are not followed.
- Type aliases (`type A = B`) are not components.
"""

import logging
import os
import posixpath
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from tree_sitter_language_pack import get_parser

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node

logger = logging.getLogger(__name__)

# `component_type` / `node_type` per underlying type of a `type_spec`.
_TYPE_MAPPING = {
    "struct_type": ("struct", "struct"),
    "interface_type": ("interface", "interface"),
}
_DEFINED_TYPE = ("class", "type")

_FUNCTION_SCOPES = ("function_declaration", "method_declaration")

# Predeclared types (and `any`/`comparable`) never become edges.
GO_PRIMITIVE_TYPES = frozenset(
    {
        "bool",
        "byte",
        "rune",
        "string",
        "error",
        "any",
        "comparable",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uintptr",
        "float32",
        "float64",
        "complex64",
        "complex128",
    }
)

# Predeclared functions.
GO_BUILTIN_FUNCS = frozenset(
    {
        "append",
        "cap",
        "clear",
        "close",
        "complex",
        "copy",
        "delete",
        "imag",
        "len",
        "make",
        "max",
        "min",
        "new",
        "panic",
        "print",
        "println",
        "real",
        "recover",
    }
)

# Standard-library method names. A call whose receiver type is unknown and
# whose only signal is one of these names is dropped instead of being
# matched by name against the repository's methods.
GO_COMMON_METHODS = frozenset(
    {
        "Error",
        "String",
        "GoString",
        "Format",
        "Unwrap",
        "Is",
        "As",
        "Len",
        "Cap",
        "Less",
        "Swap",
        "Close",
        "Read",
        "ReadByte",
        "ReadRune",
        "ReadString",
        "ReadBytes",
        "ReadFrom",
        "Write",
        "WriteByte",
        "WriteRune",
        "WriteString",
        "WriteTo",
        "Seek",
        "Sync",
        "Flush",
        "Bytes",
        "Reset",
        "Grow",
        "Truncate",
        "Lock",
        "Unlock",
        "RLock",
        "RUnlock",
        "TryLock",
        "Add",
        "Done",
        "Wait",
        "Load",
        "Store",
        "CompareAndSwap",
        "Err",
        "Value",
        "Deadline",
        "Printf",
        "Println",
        "Print",
        "Sprintf",
        "Fprintf",
        "Errorf",
        "Fatal",
        "Fatalf",
        "Helper",
        "Log",
        "Logf",
        "Scan",
        "Text",
        "Next",
        "Name",
        "Size",
        "Stat",
        "Kind",
        "Type",
        "Elem",
        "Interface",
        "Int",
        "Uint",
        "Float",
        "Bool",
    }
)

_PACKAGE_REF_FILES = "*.go"
_MODULE_RE = re.compile(r"^\s*module\s+\"?([^\s\"]+)\"?", re.MULTILINE)
_REPLACE_RE = re.compile(r"^\s*(?:replace\s+)?\"?([^\s\"(]+)\"?(?:\s+\S+)?\s*=>\s*\"?([^\s\"]+)")
_PACKAGE_CLAUSE_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)", re.MULTILINE)
_MAJOR_VERSION_RE = re.compile(r"^v\d+$")


# ----------------------------------------------------------------------
# Package references
# ----------------------------------------------------------------------


def go_package_ref(package_dir: str, name: str) -> str:
    """`"sub/*.go::Name"`: declaration `Name` in the package at repo dir `sub`."""
    prefix = f"{package_dir}/" if package_dir else ""
    return f"{prefix}{_PACKAGE_REF_FILES}::{name}"


def split_go_package_ref(callee: str) -> tuple[str, str] | None:
    head, sep, name = callee.partition("::")
    if not sep or not name:
        return None
    if head == _PACKAGE_REF_FILES:
        return "", name
    suffix = f"/{_PACKAGE_REF_FILES}"
    if head.endswith(suffix):
        return head[: -len(suffix)], name
    return None


def is_skipped_go_file(relative_path: str) -> bool:
    """`_test.go` files and vendored dependencies yield no components."""
    posix = Path(relative_path).as_posix()
    return posix.endswith("_test.go") or "vendor" in posix.split("/")[:-1]


def package_dir_of(relative_path: str) -> str:
    return posixpath.dirname(Path(relative_path).as_posix())


# ----------------------------------------------------------------------
# go.mod
# ----------------------------------------------------------------------


@lru_cache(maxsize=256)
def _parse_go_mod(path: str, mtime_ns: int) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """Module path and local `replace` targets of a go.mod file."""
    del mtime_ns  # cache key only
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, ()
    match = _MODULE_RE.search(text)
    module = match.group(1) if match else None
    replaces = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("replace") and line.rstrip().endswith("("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if not (in_block or line.startswith("replace")):
            continue
        m = _REPLACE_RE.match(line)
        if m and m.group(2).startswith(("./", "../")):
            replaces.append((m.group(1), m.group(2)))
    return module, tuple(replaces)


def _read_go_mod(path: str):
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None, ()
    return _parse_go_mod(path, mtime)


def _normalize_dir(path: str) -> str | None:
    norm = posixpath.normpath(path)
    if norm == ".":
        return ""
    if norm.startswith(".."):
        return None
    return norm


@lru_cache(maxsize=1024)
def _package_name_in_dir(abs_dir: str) -> str | None:
    """The package clause of the first non-test Go file in a directory."""
    try:
        names = sorted(os.listdir(abs_dir))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".go") or name.endswith("_test.go"):
            continue
        try:
            with open(os.path.join(abs_dir, name), encoding="utf-8", errors="replace") as f:
                head = f.read(8192)
        except OSError:
            continue
        match = _PACKAGE_CLAUSE_RE.search(re.sub(r"//[^\n]*", "", head))
        if match:
            return match.group(1)
    return None


def _default_import_names(import_path: str) -> set[str]:
    """Likely package names of an import path without an explicit alias."""
    parts = [p for p in import_path.split("/") if p]
    if not parts:
        return set()
    last = parts[-1]
    if _MAJOR_VERSION_RE.match(last) and len(parts) > 1:
        last = parts[-2]
    last = re.sub(r"\.v\d+$", "", last)
    names = {last}
    for prefix in ("go-", "go."):
        if last.startswith(prefix):
            names.add(last[len(prefix) :])
    for suffix in ("-go", ".go"):
        if last.endswith(suffix):
            names.add(last[: -len(suffix)])
    names |= {n.replace("-", "") for n in names} | {n.replace("-", "_") for n in names}
    return {n for n in names if n.isidentifier()}


# ----------------------------------------------------------------------
# Analyzer
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def _go_parser():
    return get_parser("go")


class TreeSitterGoAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        # Same-file symbol table keyed by logical name ("Foo", "Foo.Bar").
        self.top_level_nodes: dict = {}
        self.seen_relationships: set = set()
        self.relative_path = Path(self._get_relative_path()).as_posix()
        self.package_dir = package_dir_of(self.relative_path)
        # Import name -> repository dir of the package, or None if external.
        self.imports: dict[str, str | None] = {}
        # The caller's own package plus the repository packages it imports.
        self.visible_dirs: set[str] = {self.package_dir}
        self._struct_fields: dict[str, dict] = {}
        self._func_results: dict[str, object] = {}
        self._package_vars: dict[str, tuple[str | None, str] | None] = {}
        self._bindings_cache: dict = {}
        if is_skipped_go_file(self.relative_path):
            return
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
            tree = _go_parser().parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            self._collect_imports(root)
            self._extract_nodes(root, lines)
            self._collect_package_vars(root)
            self._extract_relationships(root)
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Go file {self.file_path}: {e}")

    @staticmethod
    def _text(node) -> str:
        return node.text.decode("utf8", errors="replace") if node is not None else ""

    def _field_text(self, node, field_name: str) -> str | None:
        child = node.child_by_field_name(field_name)
        return self._text(child) if child is not None else None

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def _collect_imports(self, root):
        for child in root.children:
            if child.type == "import_declaration":
                for spec in self._descendants(child, "import_spec"):
                    self._register_import(spec)

    def _register_import(self, spec):
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            return
        import_path = self._text(path_node).strip('"`')
        alias_node = spec.child_by_field_name("name")
        alias = self._text(alias_node) if alias_node is not None else None
        if alias in ("_", "."):
            return
        repo_dir = self._resolve_import_dir(import_path)
        if repo_dir is not None:
            self.visible_dirs.add(repo_dir)
        if alias:
            names = {alias}
        else:
            names = _default_import_names(import_path)
            if repo_dir is not None and self.repo_path:
                actual = _package_name_in_dir(os.path.join(self.repo_path, repo_dir))
                if actual:
                    names = {actual}
        for name in names:
            # A repository package wins over an external one of the same name.
            if self.imports.get(name) is None:
                self.imports[name] = repo_dir

    def _resolve_import_dir(self, import_path: str) -> str | None:
        """Repository dir of an imported package, or None when external."""
        if not self.repo_path:
            return None
        directory = self.package_dir
        while True:
            go_mod = os.path.join(self.repo_path, directory, "go.mod")
            if os.path.isfile(go_mod):
                return self._import_dir_from_go_mod(import_path, directory, go_mod)
            if not directory:
                break
            directory = posixpath.dirname(directory)
        return self._import_dir_without_go_mod(import_path)

    @staticmethod
    def _import_dir_from_go_mod(import_path: str, mod_dir: str, go_mod: str) -> str | None:
        module, replaces = _read_go_mod(go_mod)
        targets = [(module, "")] if module else []
        targets += [(old, new) for old, new in replaces]
        for prefix, local in targets:
            if import_path == prefix or import_path.startswith(prefix + "/"):
                rest = import_path[len(prefix) :].lstrip("/")
                return _normalize_dir(posixpath.join(mod_dir, local, rest))
        return None

    def _import_dir_without_go_mod(self, import_path: str) -> str | None:
        """No go.mod above the file (GOPATH layout or a sub-tree was
        analyzed): match the longest import-path suffix that is a repo dir.
        Standard-library paths (no dot in the first element) never match."""
        parts = import_path.split("/")
        if len(parts) < 2 or "." not in parts[0]:
            return None
        for start in range(1, len(parts)):
            candidate = "/".join(parts[start:])
            if os.path.isdir(os.path.join(self.repo_path, candidate)):
                return candidate
        return None

    # ------------------------------------------------------------------
    # Pass 1: components
    # ------------------------------------------------------------------

    def _extract_nodes(self, root, lines):
        for child in root.children:
            if child.type == "function_declaration":
                self._add_function(child, lines)
            elif child.type == "method_declaration":
                self._add_method(child, lines)
            elif child.type == "type_declaration":
                specs = [c for c in child.named_children if c.type == "type_spec"]
                grouped = any(c.type == "(" for c in child.children)
                for spec in specs:
                    self._add_type(spec, child if not grouped else spec, lines)

    def _add_function(self, node, lines):
        name = self._field_text(node, "name")
        if not name or name == "_":
            return
        result = node.child_by_field_name("result")
        if result is not None:
            self._func_results.setdefault(name, result)
        self._add_node(
            node,
            name,
            "function",
            "function",
            lines,
            parameters=self._extract_parameters(node),
        )

    def _add_method(self, node, lines):
        name = self._field_text(node, "name")
        owner = self._receiver_type_name(node)
        if not name or not owner or name == "_":
            return
        logical = f"{owner}.{name}"
        result = node.child_by_field_name("result")
        if result is not None:
            self._func_results.setdefault(logical, result)
        self._add_node(
            node,
            logical,
            "method",
            "method",
            lines,
            class_name=owner,
            parameters=self._extract_parameters(node),
        )

    def _receiver_type_name(self, method_node) -> str | None:
        receiver = method_node.child_by_field_name("receiver")
        if receiver is None:
            return None
        for param in receiver.named_children:
            if param.type == "parameter_declaration":
                ref = self._primary_type_ref(param.child_by_field_name("type"))
                if ref and ref[0] is None:
                    return ref[1]
        return None

    def _add_type(self, spec, outer, lines):
        name = self._field_text(spec, "name")
        type_node = spec.child_by_field_name("type")
        if not name or name == "_" or type_node is None:
            return
        component_type, node_type = _TYPE_MAPPING.get(type_node.type, _DEFINED_TYPE)
        node_obj = self._add_node(outer, name, component_type, node_type, lines)
        if node_obj is None:
            return

        bases: list[str] = []
        if type_node.type == "struct_type":
            fields: dict = {}
            for decl in self._descendants(type_node, "field_declaration", stop_at_nested=True):
                type_child = decl.child_by_field_name("type")
                names = decl.children_by_field_name("name")
                if not names:
                    embedded = self._primary_type_ref(type_child)
                    if embedded:
                        qualifier, base = embedded
                        bases.append(f"{qualifier}.{base}" if qualifier else base)
                        fields.setdefault(base, type_child)
                for field_name in names:
                    fields[self._text(field_name)] = type_child
            self._struct_fields[name] = fields
        elif type_node.type == "interface_type":
            for elem in type_node.named_children:
                if elem.type == "type_elem":
                    for ref in self._collect_type_refs(elem):
                        qualifier, base = ref
                        bases.append(f"{qualifier}.{base}" if qualifier else base)
                elif elem.type in ("method_elem", "method_spec"):
                    method = self._field_text(elem, "name")
                    if method:
                        self._add_node(
                            elem,
                            f"{name}.{method}",
                            "method",
                            "method",
                            lines,
                            class_name=name,
                            parameters=self._extract_parameters(elem),
                        )
        if bases:
            node_obj.base_classes = bases

    def _extract_parameters(self, func_node) -> list[str] | None:
        params_node = func_node.child_by_field_name("parameters")
        if params_node is None:
            return None
        params = [
            self._text(child).strip()
            for child in params_node.named_children
            if child.type in ("parameter_declaration", "variadic_parameter_declaration")
        ]
        return params or None

    def _extract_docstring(self, node) -> str:
        """Contiguous comment lines directly above a declaration."""
        comments = []
        expected_line = node.start_point[0] - 1
        sibling = node.prev_sibling
        while (
            sibling is not None
            and sibling.type == "comment"
            and sibling.end_point[0] == expected_line
        ):
            comments.append(self._text(sibling).strip())
            expected_line = sibling.start_point[0] - 1
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
    ) -> Node | None:
        if logical_name in self.top_level_nodes:
            # A second `func init()` in the same file: keep the first.
            return None
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
            language="go",
        )
        self.nodes.append(node_obj)
        self.top_level_nodes[logical_name] = node_obj
        return node_obj

    def _collect_package_vars(self, root):
        """Types of package-level `var`s declared in this file."""
        for child in root.children:
            if child.type != "var_declaration":
                continue
            for spec in self._descendants(child, "var_spec"):
                for name, ref in self._var_spec_bindings(spec):
                    self._package_vars[name] = ref

    # ------------------------------------------------------------------
    # Pass 2: relationships
    # ------------------------------------------------------------------

    def _extract_relationships(self, node):
        if node.type == "type_spec" and node.parent is not None:
            if node.parent.parent is not None and node.parent.parent.type == "source_file":
                self._emit_type_spec_edges(node)
        elif node.type == "composite_literal":
            self._emit_instantiation_edge(node)
        elif node.type == "call_expression":
            self._emit_call_edge(node)
        elif node.type == "type_conversion_expression":
            self._emit_generic_call_edge(node)
        elif node.type in ("parameter_declaration", "variadic_parameter_declaration"):
            if not self._is_receiver(node):
                self._emit_type_use_edges(node, node.child_by_field_name("type"))
        elif node.type in ("var_spec", "type_assertion_expression"):
            self._emit_type_use_edges(node, node.child_by_field_name("type"))
        elif node.type in ("function_declaration", "method_declaration", "func_literal"):
            result = node.child_by_field_name("result")
            if result is not None and result.type != "parameter_list":
                self._emit_type_use_edges(result, result)

        for child in node.children:
            self._extract_relationships(child)

    @staticmethod
    def _descendants(node, node_type: str, stop_at_nested: bool = False):
        """Descendants of a given type; with `stop_at_nested`, do not look
        inside a match (fields of an anonymous nested struct stay with it)."""
        found = []
        stack = list(reversed(node.children))
        while stack:
            n = stack.pop()
            if n.type == node_type:
                found.append(n)
                if stop_at_nested:
                    continue
            stack.extend(reversed(n.children))
        return found

    def _primary_type_ref(self, type_node) -> tuple[str | None, str] | None:
        """`(package alias or None, type name)` of a type, stripping
        pointers, parentheses, and type arguments. Slices, arrays, and maps
        give their element type, so `xs[i].M()` and `range xs` values type
        their method calls."""
        while type_node is not None and type_node.type in (
            "pointer_type",
            "parenthesized_type",
            "generic_type",
            "type_elem",
            "slice_type",
            "array_type",
            "map_type",
        ):
            if type_node.type == "generic_type":
                type_node = type_node.child_by_field_name("type")
            elif type_node.type in ("slice_type", "array_type"):
                type_node = type_node.child_by_field_name("element")
            elif type_node.type == "map_type":
                type_node = type_node.child_by_field_name("value")
            else:
                type_node = type_node.named_children[0] if type_node.named_children else None
        if type_node is None:
            return None
        if type_node.type == "type_identifier":
            return None, self._text(type_node)
        if type_node.type == "qualified_type":
            package = self._field_text(type_node, "package")
            name = self._field_text(type_node, "name")
            return (package, name) if package and name else None
        return None

    def _collect_type_refs(self, type_node) -> list[tuple[str | None, str]]:
        """Every named type a type expression mentions (`map[string]*Event`
        and `[]sub.Item` still yield `Event` and `sub.Item`), minus
        predeclared types and type parameters in scope."""
        refs: list[tuple[str | None, str]] = []
        if type_node is None:
            return refs
        generics = self._type_params_in_scope(type_node)
        stack = [type_node]
        while stack:
            n = stack.pop()
            ref = None
            if n.type == "qualified_type":
                ref = self._primary_type_ref(n)
            elif n.type == "type_identifier":
                name = self._text(n)
                if name not in GO_PRIMITIVE_TYPES and name not in generics:
                    ref = (None, name)
            else:
                stack.extend(reversed(n.children))
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def _type_params_in_scope(self, node) -> set[str]:
        """Type parameter names of the declarations enclosing a node,
        including those a generic receiver (`func (b *Buffer[T])`) binds."""
        params: set[str] = set()
        current = node
        while current is not None:
            type_params = current.child_by_field_name("type_parameters")
            if type_params is not None:
                for decl in type_params.named_children:
                    for name in decl.children_by_field_name("name"):
                        params.add(self._text(name))
            if current.type == "method_declaration":
                receiver = current.child_by_field_name("receiver")
                for generic in self._descendants(receiver, "generic_type") if receiver else []:
                    args = generic.child_by_field_name("type_arguments")
                    for ident in self._descendants(args, "type_identifier") if args else []:
                        params.add(self._text(ident))
            current = current.parent
        return params

    def _type_ref_target(self, ref) -> tuple[str, bool] | None:
        """`(callee, resolved)` for a type reference, or None when it points
        outside the repository."""
        qualifier, name = ref
        if qualifier is None:
            node_obj = self.top_level_nodes.get(name)
            if node_obj is not None:
                return node_obj.id, True
            if name in GO_PRIMITIVE_TYPES:
                return None
            return go_package_ref(self.package_dir, name), False
        repo_dir = self.imports.get(qualifier)
        if repo_dir is None:
            return None
        return go_package_ref(repo_dir, name), False

    def _add_type_ref_relationship(self, caller_id: str, ref, line: int):
        target = self._type_ref_target(ref)
        if target:
            self._add_relationship_raw(caller_id, target[0], line, target[1])

    def _emit_type_spec_edges(self, spec):
        name = self._field_text(spec, "name")
        type_node = spec.child_by_field_name("type")
        owner = self.top_level_nodes.get(name) if name else None
        if owner is None or type_node is None:
            return
        if type_node.type == "struct_type":
            for decl in self._descendants(type_node, "field_declaration"):
                for ref in self._collect_type_refs(decl.child_by_field_name("type")):
                    self._add_type_ref_relationship(owner.id, ref, decl.start_point[0] + 1)
        elif type_node.type == "interface_type":
            for elem in type_node.named_children:
                if elem.type == "type_elem":
                    for ref in self._collect_type_refs(elem):
                        self._add_type_ref_relationship(owner.id, ref, elem.start_point[0] + 1)
        else:
            for ref in self._collect_type_refs(type_node):
                self._add_type_ref_relationship(owner.id, ref, spec.start_point[0] + 1)

    # -- callers --------------------------------------------------------

    def _find_containing_function(self, node):
        current = node.parent
        while current is not None:
            if current.type in _FUNCTION_SCOPES:
                return current
            current = current.parent
        return None

    def _function_logical_name(self, func_node) -> str | None:
        name = self._field_text(func_node, "name")
        if not name:
            return None
        if func_node.type == "method_declaration":
            owner = self._receiver_type_name(func_node)
            return f"{owner}.{name}" if owner else None
        return name

    def _resolve_caller(self, node) -> str | None:
        func_node = self._find_containing_function(node)
        if func_node is None:
            return None
        logical = self._function_logical_name(func_node)
        node_obj = self.top_level_nodes.get(logical) if logical else None
        return node_obj.id if node_obj is not None else None

    # -- type uses ------------------------------------------------------

    @staticmethod
    def _is_receiver(param) -> bool:
        params = param.parent
        owner = params.parent if params is not None else None
        if owner is None or owner.type != "method_declaration":
            return False
        receiver = owner.child_by_field_name("receiver")
        return receiver is not None and receiver.start_byte == params.start_byte

    def _emit_type_use_edges(self, anchor, type_node):
        """Types a function names in its signature, closures, local `var`s,
        and type assertions: it cannot be ported before them."""
        caller_id = self._resolve_caller(anchor)
        if not caller_id or type_node is None:
            return
        for ref in self._collect_type_refs(type_node):
            self._add_type_ref_relationship(caller_id, ref, anchor.start_point[0] + 1)

    # -- instantiation --------------------------------------------------

    def _emit_instantiation_edge(self, literal):
        caller_id = self._resolve_caller(literal)
        if not caller_id:
            return
        type_node = literal.child_by_field_name("type")
        ref = self._primary_type_ref(type_node)
        # `[]Event{...}` / `map[string]Event{...}` instantiate their element types.
        refs = [ref] if ref else self._collect_type_refs(type_node)
        generics = self._type_params_in_scope(literal)
        for ref in refs:
            if ref[1] not in generics:
                self._add_type_ref_relationship(caller_id, ref, literal.start_point[0] + 1)

    # -- calls ----------------------------------------------------------

    def _emit_call_edge(self, call_node):
        caller_id = self._resolve_caller(call_node)
        if not caller_id:
            return
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            return
        line = call_node.start_point[0] + 1
        if func_node.type == "identifier":
            self._emit_bare_call_edge(caller_id, self._text(func_node), call_node, line)
        elif func_node.type == "selector_expression":
            self._emit_selector_call_edge(caller_id, func_node, call_node, line)
        elif func_node.type == "parenthesized_expression":
            # Conversion to a pointer type: `(*stringHeader)(unsafe.Pointer(&s))`.
            inner = func_node.named_children[0] if func_node.named_children else None
            if inner is not None and inner.type == "unary_expression":
                inner = inner.child_by_field_name("operand")
            if inner is not None and inner.type == "identifier":
                target = self.top_level_nodes.get(self._text(inner))
                if target is not None and target.component_type != "function":
                    self._add_relationship_raw(caller_id, target.id, line, True)
        elif func_node.type == "index_expression":
            # Explicitly instantiated generic function: `Map[int](xs)`.
            operand = func_node.child_by_field_name("operand")
            if operand is not None and operand.type == "identifier":
                self._emit_bare_call_edge(caller_id, self._text(operand), call_node, line)

    def _emit_generic_call_edge(self, node):
        """`f[int](x)` parses as a conversion to a generic type."""
        caller_id = self._resolve_caller(node)
        type_node = node.child_by_field_name("type")
        if not caller_id or type_node is None:
            return
        ref = self._primary_type_ref(type_node)
        if not ref:
            return
        line = node.start_point[0] + 1
        if ref[0] is None:
            self._emit_bare_call_edge(caller_id, ref[1], node, line)
        else:
            self._add_type_ref_relationship(caller_id, ref, line)

    def _emit_bare_call_edge(self, caller_id: str, name: str, call_node, line: int):
        found, _ = self._local_binding(call_node, name)
        if found:
            return  # a function value held in a local or parameter
        node_obj = self.top_level_nodes.get(name)
        if node_obj is not None:
            self._add_relationship_raw(caller_id, node_obj.id, line, True)
            return
        if name in GO_BUILTIN_FUNCS:
            if name in ("new", "make"):
                args = call_node.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                for ref in self._collect_type_refs(first):
                    self._add_type_ref_relationship(caller_id, ref, line)
            return
        if name in GO_PRIMITIVE_TYPES or name in self._type_params_in_scope(call_node):
            return
        if name in self._package_vars:
            return  # a package-level function variable
        self._add_relationship_raw(caller_id, go_package_ref(self.package_dir, name), line, False)

    def _emit_selector_call_edge(self, caller_id: str, func_node, call_node, line: int):
        operand = func_node.child_by_field_name("operand")
        field = func_node.child_by_field_name("field")
        if operand is None or field is None:
            return
        method = self._text(field)

        if operand.type == "identifier":
            ident = self._text(operand)
            found, ref = self._local_binding(call_node, ident)
            if found:
                self._emit_typed_method_edge(caller_id, ref, method, line)
                return
            if ident in self.imports:
                repo_dir = self.imports[ident]
                if repo_dir is not None:
                    self._add_relationship_raw(
                        caller_id, go_package_ref(repo_dir, method), line, False
                    )
                return
            if ident in self._package_vars:
                self._emit_typed_method_edge(caller_id, self._package_vars[ident], method, line)
                return
            if ident in self.top_level_nodes or ident[:1].isupper():
                # Method expression `Buffer.Push(b)`.
                self._emit_typed_method_edge(caller_id, (None, ident), method, line)
                return
            self._emit_typed_method_edge(caller_id, None, method, line)
            return

        self._emit_typed_method_edge(caller_id, self._expression_type(operand), method, line)

    def _emit_typed_method_edge(self, caller_id: str, ref, method: str, line: int):
        if ref is None:
            if method not in GO_COMMON_METHODS:
                self._add_relationship_raw(caller_id, method, line, False)
            return
        qualifier, type_name = ref
        logical = f"{type_name}.{method}"
        if qualifier is None:
            node_obj = self.top_level_nodes.get(logical)
            if node_obj is not None:
                self._add_relationship_raw(caller_id, node_obj.id, line, True)
                return
            if type_name in GO_PRIMITIVE_TYPES:
                return
            self._add_relationship_raw(
                caller_id, go_package_ref(self.package_dir, logical), line, False
            )
            return
        repo_dir = self.imports.get(qualifier)
        if repo_dir is not None:
            self._add_relationship_raw(caller_id, go_package_ref(repo_dir, logical), line, False)

    # -- local types ----------------------------------------------------

    def _expression_type(self, expr) -> tuple[str | None, str] | None:
        """Best-effort type of an expression used as a method receiver."""
        if expr is None:
            return None
        if expr.type == "parenthesized_expression":
            return self._expression_type(expr.named_children[0] if expr.named_children else None)
        if expr.type in ("unary_expression", "index_expression", "slice_expression"):
            return self._expression_type(expr.child_by_field_name("operand"))
        if expr.type == "identifier":
            found, ref = self._local_binding(expr, self._text(expr))
            if found:
                return ref
            return self._package_vars.get(self._text(expr))
        if expr.type in ("composite_literal", "type_assertion_expression"):
            return self._primary_type_ref(expr.child_by_field_name("type"))
        if expr.type == "selector_expression":
            return self._field_type(expr)
        if expr.type == "call_expression":
            return self._call_result_type(expr)
        return None

    def _field_type(self, selector) -> tuple[str | None, str] | None:
        """Type of `x.field` when `x`'s struct is declared in this file."""
        owner = self._expression_type(selector.child_by_field_name("operand"))
        field = selector.child_by_field_name("field")
        if owner is None or owner[0] is not None or field is None:
            return None
        type_node = self._struct_fields.get(owner[1], {}).get(self._text(field))
        return self._primary_type_ref(type_node)

    def _call_result_type(self, call, index: int = 0) -> tuple[str | None, str] | None:
        """Type of the `index`-th result of a call to a same-file function
        or method (or of `new(T)` / a conversion `Kind(x)`)."""
        func = call.child_by_field_name("function")
        if func is None:
            return None
        if func.type == "identifier":
            name = self._text(func)
            if name == "new":
                args = call.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                return self._primary_type_ref(first)
            if name in self.top_level_nodes and self.top_level_nodes[name].node_type != "function":
                return None, name  # conversion `Kind(x)`
            return self._result_ref(self._func_results.get(name), index)
        if func.type == "selector_expression":
            receiver = self._expression_type(func.child_by_field_name("operand"))
            field = func.child_by_field_name("field")
            if receiver and receiver[0] is None and field is not None:
                return self._result_ref(
                    self._func_results.get(f"{receiver[1]}.{self._text(field)}"), index
                )
        return None

    def _result_ref(self, result, index: int = 0) -> tuple[str | None, str] | None:
        if result is None:
            return None
        if result.type != "parameter_list":
            return self._primary_type_ref(result) if index == 0 else None
        # `(int, Result, bool)` or `(n, m int, r Result)`: one slot per name.
        slots = []
        for decl in result.named_children:
            if decl.type == "parameter_declaration":
                type_node = decl.child_by_field_name("type")
                slots += [type_node] * max(1, len(decl.children_by_field_name("name")))
        return self._primary_type_ref(slots[index]) if index < len(slots) else None

    def _var_spec_bindings(self, spec) -> list[tuple[str, tuple[str | None, str] | None]]:
        names = [self._text(n) for n in spec.children_by_field_name("name")]
        declared = self._primary_type_ref(spec.child_by_field_name("type"))
        if declared or spec.child_by_field_name("type") is not None:
            return [(n, declared) for n in names]
        value = spec.child_by_field_name("value")
        values = value.named_children if value is not None else []
        return [
            (n, self._expression_type(values[i]) if len(values) == len(names) else None)
            for i, n in enumerate(names)
        ]

    def _function_bindings(self, func_node) -> dict[str, list[tuple[int, object]]]:
        """Every name a function binds (receiver, parameters, `var`,
        `:=`, range and type-switch variables, closure parameters), with the
        byte offset where its scope starts (the end of the declaring
        statement, so `res := res.Get(k)` still sees the outer `res`) and its
        inferred type (or None)."""
        key = (func_node.start_byte, func_node.end_byte)
        cached = self._bindings_cache.get(key)
        if cached is not None:
            return cached
        bindings: dict[str, list[tuple[int, object]]] = defaultdict(list)
        self._bindings_cache[key] = bindings

        # Document order, so a binding's initializer can see earlier ones.
        stack = [func_node]
        while stack:
            n = stack.pop()
            stack.extend(reversed(n.children))
            if n.type in ("parameter_declaration", "variadic_parameter_declaration"):
                ref = self._primary_type_ref(n.child_by_field_name("type"))
                for name in n.children_by_field_name("name"):
                    bindings[self._text(name)].append((n.end_byte, ref))
            elif n.type in ("var_spec", "const_spec"):
                for name, ref in self._var_spec_bindings(n):
                    bindings[name].append((n.end_byte, ref))
            elif n.type == "short_var_declaration":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                names = [c for c in left.named_children] if left is not None else []
                values = right.named_children if right is not None else []
                for i, name in enumerate(names):
                    ref = None
                    if len(values) == len(names):
                        ref = self._expression_type(values[i])
                    elif len(values) == 1 and values[0].type == "call_expression":
                        ref = self._call_result_type(values[0], i)
                    bindings[self._text(name)].append((n.end_byte, ref))
            elif n.type == "range_clause":
                left = n.child_by_field_name("left")
                names = left.named_children if left is not None else []
                for i, name in enumerate(names):
                    # `for _, v := range xs`: v is an element of xs.
                    ref = None
                    if i == 1:
                        ref = self._expression_type(n.child_by_field_name("right"))
                    bindings[self._text(name)].append((n.end_byte, ref))
            elif n.type == "type_switch_statement":
                alias = n.child_by_field_name("alias")
                for name in alias.named_children if alias is not None else []:
                    bindings[self._text(name)].append((alias.end_byte, None))
        return bindings

    def _local_binding(self, use_node, name: str) -> tuple[bool, tuple[str | None, str] | None]:
        """Whether `name` is a local of the enclosing function at `use_node`,
        and its inferred type: the closest binding that precedes the use."""
        func_node = self._find_containing_function(use_node)
        if func_node is None:
            return False, None
        best = None
        for start, ref in self._function_bindings(func_node).get(name, []):
            if start <= use_node.start_byte and (best is None or start > best[0]):
                best = (start, ref)
        if best is None:
            return False, None
        return True, best[1]

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

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


def analyze_go_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterGoAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships


# ----------------------------------------------------------------------
# Repository-wide resolution (used by CallGraphAnalyzer)
# ----------------------------------------------------------------------


class GoPackageIndex:
    """Go components grouped by package directory."""

    def __init__(self, functions: dict[str, Node]):
        self.functions = functions
        self.by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.methods: dict[tuple[str, str], list[str]] = defaultdict(list)
        for func_id, node in functions.items():
            if node.language != "go":
                continue
            directory = package_dir_of(node.relative_path)
            self.by_name[(directory, node.name)].append(func_id)
            if node.component_type == "method" and "." in node.name:
                self.methods[(directory, node.name.rsplit(".", 1)[1])].append(func_id)
        for ids in (*self.by_name.values(), *self.methods.values()):
            ids.sort()

    def lookup(self, directory: str, name: str, depth: int = 0) -> str | None:
        """A declaration of the package. Several hits can only come from
        build-constrained files (`x_linux.go`, `x_windows.go`): take the
        first. `Type.Method` falls back to methods promoted from embedded
        types of the same package."""
        ids = self.by_name.get((directory, name))
        if ids:
            return ids[0]
        if "." not in name or depth >= 3:
            return None
        owner, method = name.split(".", 1)
        for owner_id in self.by_name.get((directory, owner), []):
            for base in self.functions[owner_id].base_classes or []:
                if "." in base:
                    continue
                found = self.lookup(directory, f"{base}.{method}", depth + 1)
                if found:
                    return found
        return None


def resolve_go_callee(
    callee: str, caller: Node, index: GoPackageIndex, visible_dirs: set[str] | None
) -> str | None:
    """Resolve a Go analyzer callee: a package reference names its package;
    a bare method name (receiver type unknown) resolves when exactly one
    method of that name exists in the packages the caller can see."""
    ref = split_go_package_ref(callee)
    if ref is not None:
        return index.lookup(*ref)
    if "::" in callee or "." in callee:
        return None
    dirs = visible_dirs or {package_dir_of(caller.relative_path)}
    candidates = sorted({fid for d in dirs for fid in index.methods.get((d, callee), [])})
    return candidates[0] if len(candidates) == 1 else None


def link_go_interfaces(functions: dict[str, Node]) -> list[CallRelationship]:
    """Link each named type to the interfaces of its own package whose
    methods it declares (Go implements interfaces implicitly). The
    interface is also recorded as a base class of the type."""
    types: dict[str, dict[str, Node]] = defaultdict(dict)
    method_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for node in functions.values():
        if node.language != "go":
            continue
        directory = package_dir_of(node.relative_path)
        if node.component_type == "method" and node.class_name:
            method_sets[(directory, node.class_name)].add(node.name.rsplit(".", 1)[-1])
        elif node.node_type in ("struct", "interface", "type"):
            types[directory][node.name] = node

    relationships: list[CallRelationship] = []
    for directory, by_name in types.items():
        interfaces = [
            (iface, method_sets.get((directory, iface.name)))
            for iface in by_name.values()
            if iface.node_type == "interface"
        ]
        interfaces = [(iface, methods) for iface, methods in interfaces if methods]
        if not interfaces:
            continue
        for type_node in by_name.values():
            if type_node.node_type == "interface":
                continue
            own = method_sets.get((directory, type_node.name))
            if not own:
                continue
            for iface, methods in interfaces:
                if methods <= own:
                    bases = type_node.base_classes or []
                    if iface.name not in bases:
                        type_node.base_classes = [*bases, iface.name]
                    relationships.append(
                        CallRelationship(
                            caller=type_node.id,
                            callee=iface.id,
                            call_line=type_node.start_line,
                            is_resolved=True,
                        )
                    )
    return relationships
