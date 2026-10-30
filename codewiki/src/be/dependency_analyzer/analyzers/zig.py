"""Tree-sitter based Zig analyzer.

Extracts functions, containers (`struct`, `enum`, `union`, `opaque`), their
member functions, and generic type functions (`fn List(comptime T: type)
type { return struct { ... }; }`) as documentable components, plus call,
instantiation (`T{ ... }`), and type-reference (parameters, return types,
fields, annotated locals) dependency edges.

Component types follow the same two-level `component_type` / `node_type`
split as the Go and Rust analyzers: `component_type` is the coarse type the
pipeline gates leaf selection on (structs stay "struct", other containers
map to "class"), while `node_type` and `display_name` keep the Zig
construct.

A Zig file is itself a struct, so every name a file uses is declared in it:
a top-level declaration, a member of an enclosing container, a local, or an
alias (`const util = @import("util.zig");`, `const Point =
@import("geo.zig").Point;`, `const T = util.T;`). Members of a container are
keyed by its logical name (`Point.add`, `Outer.Inner.run`), joined with `.`
because component ids already use `::` as the path separator, and
`const Self = @This();` names the enclosing container.

Imports of repository files are resolved relative to the importing file;
module names registered in the root `build.zig` with
`b.addModule("name", .{ .root_source_file = ... })` map to their root file.
Everything else (`std`, `builtin`, `root`, third-party packages, `.zon`
files) is external, and calls through it are dropped. Calls into another
file are emitted as that file's component id (`src/util.zig::sum`) and left
to `CallGraphAnalyzer`'s exact-id resolution.

Known limitations:
- `test` blocks, `comptime` blocks, and files under `zig-cache/`,
  `.zig-cache/`, or `zig-out/` produce no components or edges.
- Local types are inferred from annotations, `T{ ... }` literals, same-file
  function results, and the `T.init(...)` / `T.create(...)` convention only;
  a method call on a value of unknown type falls back to its method name,
  which resolves only when that name is unique among the Zig components.
- Field types are known for containers of the same file only, and nothing
  past the first call of a chain (`a.b().c()`) is followed.
- `usingnamespace` and comptime-computed types are not followed.
"""

import logging
import os
import posixpath
import re
from functools import lru_cache
from pathlib import Path

from tree_sitter_language_pack import get_parser

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node
from codewiki.src.be.dependency_analyzer.utils.file_selection import is_skipped_zig_file

logger = logging.getLogger(__name__)

# `component_type` / `node_type` per container keyword.
_CONTAINER_MAPPING = {
    "struct": ("struct", "struct"),
    "enum": ("class", "enum"),
    "union": ("class", "union"),
    "opaque": ("class", "opaque"),
}

_EXTERNAL_IMPORTS = {"std", "builtin", "root"}
# Constructors by convention: their result is a value of the owning type.
_CONSTRUCTOR_NAMES = {"init", "initCapacity", "create", "new", "open", "parse"}
# Tokens that may sit between a declaration's doc comment and the `Decl`.
_DECL_PREFIX_TOKENS = {"pub", "export", "extern", "inline", "noinline", "threadlocal"}

_ADD_MODULE_RE = re.compile(
    r"addModule\(\s*\"([^\"]+)\"\s*,\s*\.\{[^;]*?root_source_file\s*=\s*"
    r"(?:b\.path\(\s*|\.\{\s*\.path\s*=\s*)\"([^\"]+)\"",
    re.DOTALL,
)

# Resolution targets (plain tuples):
#   ("file", path)            a repository file used as a namespace
#   ("decl", path, logical)   a declaration of a repository file ("" = the file)
#   ("value", target | None)  a value of a known (or unknown) type
#   ("unknown",)              a value whose type cannot be inferred
#   ("external",)             std, builtin, or a third-party package
_EXTERNAL = ("external",)
_UNKNOWN = ("unknown",)


@lru_cache(maxsize=64)
def _parse_build_zig(path: str, mtime_ns: int) -> tuple[tuple[str, str], ...]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ()
    return tuple((name, posixpath.normpath(root)) for name, root in _ADD_MODULE_RE.findall(text))


def _build_zig_modules(repo_path: str) -> dict[str, str]:
    """Module name -> root source file, from `addModule` calls in build.zig."""
    if not repo_path:
        return {}
    path = os.path.join(repo_path, "build.zig")
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    return dict(_parse_build_zig(path, mtime_ns))


def _zig_parser():
    return get_parser("zig")


def _alias_chain(var_decl) -> tuple[tuple[str, str], tuple[str, ...]] | None:
    """`(base, fields)` of a `const X = @import("a.zig").B.C;` or
    `const X = a.B;` declaration: base is `("import", spec)` or
    `("name", identifier)`. None for anything else."""
    value = None
    seen_eq = False
    for child in var_decl.children:
        if child.type == "=":
            seen_eq = True
        elif seen_eq:
            value = child
            break
    if value is None or value.type != "ErrorUnionExpr" or value.named_child_count != 1:
        return None
    suffix = value.named_children[0]
    if suffix.type != "SuffixExpr" or not suffix.children:
        return None
    first, rest = suffix.children[0], list(suffix.children[1:])
    if first.type == "IDENTIFIER":
        base = ("name", first.text.decode("utf8", errors="replace"))
    elif first.type == "BUILTINIDENTIFIER" and first.text == b"@import":
        if not rest or rest[0].type != "FnCallArguments":
            return None
        literal = next((n for n in rest[0].named_children if n.type == "ErrorUnionExpr"), None)
        text = literal.text.decode("utf8", errors="replace").strip() if literal else ""
        if not (text.startswith('"') and text.endswith('"')):
            return None
        base = ("import", text.strip('"'))
        rest = rest[1:]
    else:
        return None
    fields = []
    last = base[1] if base[0] == "name" else ""
    for index, op in enumerate(rest):
        is_last = index == len(rest) - 1
        if op.type == "FnCallArguments" and is_last and _is_type_name(last):
            break  # `= KeyValue([]const u8, hash)`: an instance of a generic type
        if op.type != "FieldOrFnCall":
            return None
        field = op.child_by_field_name("field_access")
        if field is None:
            call = op.child_by_field_name("function_call")
            if call is None or not is_last or not _is_type_name(call.text.decode("utf8")):
                return None
            field = call
        last = field.text.decode("utf8", errors="replace")
        fields.append(last)
    return base, tuple(fields)


def _is_type_name(name: str) -> bool:
    """Zig names functions that return types in TitleCase (`ArrayList`)."""
    return bool(name) and name[0].isupper()


@lru_cache(maxsize=4096)
def _file_aliases(path: str, mtime_ns: int) -> dict:
    """Top-level aliases of another Zig file, for following re-exports."""
    try:
        with open(path, "rb") as handle:
            source = handle.read()
    except OSError:
        return {}
    aliases = {}
    for decl in _zig_parser().parse(source).root_node.children:
        if decl.type != "Decl":
            continue
        var_decl = next((c for c in decl.children if c.type == "VarDecl"), None)
        name = var_decl.child_by_field_name("variable_type_function") if var_decl else None
        chain = _alias_chain(var_decl) if name is not None else None
        if chain is not None:
            aliases[name.text.decode("utf8", errors="replace")] = chain
    return aliases


class _Container:
    def __init__(self, node, logical: str, parent: str | None):
        self.node = node
        self.logical = logical
        self.parent = parent
        # Member name -> logical name of a function or container component.
        self.decls: dict[str, str] = {}
        # Member name -> `VarDecl` node of a non-container constant/variable.
        self.aliases: dict[str, object] = {}
        # Field name -> its type node.
        self.fields: dict[str, object] = {}


@lru_cache(maxsize=4096)
def _file_components(path: str, mtime_ns: int, repo_path: str) -> frozenset[str]:
    """Logical names of the components another Zig file declares."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return frozenset()
    analyzer = TreeSitterZigAnalyzer(path, content, repo_path, components_only=True)
    return frozenset(analyzer.top_level_nodes)


class TreeSitterZigAnalyzer:
    def __init__(
        self,
        file_path: str,
        content: str,
        repo_path: str | None = None,
        *,
        components_only: bool = False,
    ):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        # Same-file symbol table keyed by logical name ("Foo", "Foo.bar").
        self.top_level_nodes: dict = {}
        self.seen_relationships: set = set()
        self.relative_path = Path(self._get_relative_path()).as_posix()
        self._containers: dict[str, _Container] = {}
        # Function logical name -> (return type node, container it lives in).
        self._returns: dict[str, tuple[object, str]] = {}
        self._alias_cache: dict[tuple[str, str], tuple | None] = {}
        self._return_cache: dict[str, tuple | None] = {}
        self._components_only = components_only
        if is_skipped_zig_file(self.relative_path):
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
        return f"{self.relative_path}::{logical_name}"

    def _analyze(self):
        try:
            tree = _zig_parser().parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            self._collect_container(root, "", None, lines)
            if self._components_only:
                return
            for container in list(self._containers.values()):
                self._extract_container_relationships(container)
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Zig file {self.file_path}: {e}")

    @staticmethod
    def _text(node) -> str:
        return node.text.decode("utf8", errors="replace") if node is not None else ""

    @staticmethod
    def _child(node, node_type: str):
        for child in node.children:
            if child.type == node_type:
                return child
        return None

    @staticmethod
    def _qualify(owner: str, name: str) -> str:
        return f"{owner}.{name}" if owner else name

    # ------------------------------------------------------------------
    # Pass 1: components and scopes
    # ------------------------------------------------------------------

    def _collect_container(self, node, logical: str, parent: str | None, lines):
        container = _Container(node, logical, parent)
        self._containers[logical] = container
        for child in node.children:
            if child.type == "ContainerField":
                name = child.child_by_field_name("field_member")
                type_node = self._field_type_node(child)
                if name is not None and type_node is not None:
                    container.fields[self._text(name)] = type_node
            elif child.type == "Decl":
                self._collect_decl(child, container, lines)

    def _collect_decl(self, decl, container: _Container, lines):
        proto = self._child(decl, "FnProto")
        if proto is not None:
            name_node = proto.child_by_field_name("function")
            body = self._child(decl, "Block")
            if name_node is None or body is None:
                return  # extern prototypes have no body to document
            name = self._text(name_node)
            logical = self._qualify(container.logical, name)
            container.decls[name] = logical
            return_type = self._return_type_node(proto)
            if return_type is not None:
                self._returns[logical] = (return_type, container.logical)
            generic = self._generic_container(proto, body)
            if generic is not None:
                keyword = self._container_keyword(generic)
                component_type, node_type = _CONTAINER_MAPPING.get(keyword, ("class", keyword))
                self._add_node(
                    decl,
                    logical,
                    component_type,
                    node_type,
                    lines,
                    display_name=f"generic {node_type} {logical}",
                    parameters=self._parameters(proto),
                )
                self._collect_container(generic, logical, container.logical, lines)
                return
            is_method = bool(container.logical)
            self._add_node(
                decl,
                logical,
                "method" if is_method else "function",
                "method" if is_method else "function",
                lines,
                class_name=container.logical or None,
                parameters=self._parameters(proto),
            )
            return

        var_decl = self._child(decl, "VarDecl")
        if var_decl is None:
            return
        name_node = var_decl.child_by_field_name("variable_type_function")
        if name_node is None:
            return
        name = self._text(name_node)
        value = self._var_value(var_decl)
        nested = self._container_decl(value) if value is not None else None
        if nested is None:
            container.aliases[name] = var_decl
            return
        logical = self._qualify(container.logical, name)
        container.decls[name] = logical
        keyword = self._container_keyword(nested)
        component_type, node_type = _CONTAINER_MAPPING.get(keyword, ("class", keyword))
        self._add_node(decl, logical, component_type, node_type, lines)
        self._collect_container(nested, logical, container.logical, lines)

    def _container_decl(self, expr):
        """The `ContainerDecl` an expression consists of, if any."""
        if expr is None or expr.type != "ErrorUnionExpr":
            return None
        suffix = self._child(expr, "SuffixExpr")
        if suffix is None or suffix.named_child_count != 1:
            return None
        inner = suffix.named_children[0]
        return inner if inner.type == "ContainerDecl" else None

    def _container_keyword(self, container_decl) -> str:
        decl_type = self._child(container_decl, "ContainerDeclType")
        if decl_type is None or not decl_type.children:
            return "struct"
        return decl_type.children[0].type

    def _generic_container(self, proto, body):
        """`return struct { ... };` of a function returning `type`."""
        return_type = self._return_type_node(proto)
        if return_type is None or self._text(return_type).strip() != "type":
            return None
        for statement in body.children:
            if statement.type != "Statement":
                continue
            assign = self._child(statement, "AssignExpr")
            if assign is None or not assign.children or assign.children[0].type != "return":
                continue
            nested = self._container_decl(self._child(assign, "ErrorUnionExpr"))
            if nested is not None:
                return nested
        return None

    @staticmethod
    def _return_type_node(proto):
        result = None
        for child in proto.children:
            if child.type == "ErrorUnionExpr":
                result = child
        return result

    def _parameters(self, proto) -> list[str] | None:
        params = self._child(proto, "ParamDeclList")
        if params is None:
            return None
        names = []
        for param in params.children:
            if param.type == "ParamDecl":
                name = param.child_by_field_name("parameter")
                if name is not None:
                    names.append(self._text(name))
        return names or None

    @staticmethod
    def _field_type_node(field):
        seen_colon = False
        for child in field.children:
            if child.type == ":":
                seen_colon = True
            elif seen_colon and child.type == "ErrorUnionExpr":
                return child
        return None

    @staticmethod
    def _var_type_node(var_decl):
        seen_colon = False
        for child in var_decl.children:
            if child.type == ":":
                seen_colon = True
            elif child.type == "=":
                return None
            elif seen_colon and child.type == "ErrorUnionExpr":
                return child
        return None

    @staticmethod
    def _var_value(var_decl):
        seen_eq = False
        for child in var_decl.children:
            if child.type == "=":
                seen_eq = True
            elif seen_eq and child.type in ("ErrorUnionExpr", "UnaryExpr", "BinaryExpr"):
                return child
        return None

    def _extract_docstring(self, decl) -> str:
        comments = []
        sibling = decl.prev_sibling
        while sibling is not None:
            if sibling.type == "doc_comment":
                comments.append(self._text(sibling))
            elif sibling.type not in _DECL_PREFIX_TOKENS and sibling.type != "line_comment":
                break
            sibling = sibling.prev_sibling
        lines = []
        for block in reversed(comments):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("///"):
                    lines.append(line[3:].strip())
        return "\n".join(lines)

    def _add_node(
        self,
        decl,
        logical_name: str,
        component_type: str,
        node_type: str,
        lines,
        class_name: str | None = None,
        parameters: list[str] | None = None,
        display_name: str | None = None,
    ) -> Node | None:
        if logical_name in self.top_level_nodes:
            return None
        component_id = self._get_component_id(logical_name)
        docstring = self._extract_docstring(decl)

        start_line_idx = decl.start_point[0]
        end_line_idx = decl.end_point[0] + 1
        code_snippet = (
            "\n".join(lines[start_line_idx:end_line_idx]) if start_line_idx < len(lines) else ""
        )

        node_obj = Node(
            id=component_id,
            name=logical_name,
            component_type=component_type,
            file_path=str(self.file_path),
            relative_path=self.relative_path,
            source_code=code_snippet,
            start_line=decl.start_point[0] + 1,
            end_line=decl.end_point[0] + 1,
            has_docstring=bool(docstring),
            docstring=docstring,
            parameters=parameters,
            node_type=node_type,
            class_name=class_name,
            display_name=display_name or f"{node_type} {logical_name}",
            component_id=component_id,
            language="zig",
        )
        self.nodes.append(node_obj)
        self.top_level_nodes[logical_name] = node_obj
        return node_obj

    # ------------------------------------------------------------------
    # Name resolution
    # ------------------------------------------------------------------

    def _resolve_import(self, spec: str, importer: str | None = None):
        if spec in _EXTERNAL_IMPORTS:
            return _EXTERNAL
        if not spec.endswith(".zig"):
            root = _build_zig_modules(self.repo_path).get(spec)
            return ("file", root) if root else _EXTERNAL
        importer = importer or self.relative_path
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
        if joined.startswith("../") or joined == ".." or posixpath.isabs(joined):
            return _EXTERNAL
        if self.repo_path and not os.path.isfile(os.path.join(self.repo_path, joined)):
            return _EXTERNAL
        return ("file", joined)

    def _lookup(self, name: str, scope: str, locals_: dict | None):
        if locals_ is not None and name in locals_:
            type_target = locals_[name]
            if type_target == _EXTERNAL:
                return _EXTERNAL
            return ("value", type_target) if type_target is not None else _UNKNOWN
        current: str | None = scope
        while current is not None:
            container = self._containers.get(current)
            if container is None:
                break
            if name in container.decls:
                return ("decl", self.relative_path, container.decls[name])
            if name in container.aliases:
                return self._resolve_alias(current, name)
            current = container.parent
        return None

    def _resolve_alias(self, scope: str, name: str):
        key = (scope, name)
        if key in self._alias_cache:
            return self._alias_cache[key]
        self._alias_cache[key] = None  # cycle guard
        var_decl = self._containers[scope].aliases[name]
        type_node = self._var_type_node(var_decl)
        if type_node is not None:
            result = ("value", self._type_target(type_node, scope))
        else:
            result = self._expr_target(var_decl, scope, None, None)
        self._alias_cache[key] = result
        return result

    def _reexport(self, path: str, name: str, depth: int):
        """Follow `pub const Name = @import("x.zig").Name;` (or `= x.Name`)
        in another file of the repository."""
        if depth > 8 or not self.repo_path:
            return None
        abs_path = os.path.join(self.repo_path, path)
        try:
            mtime_ns = os.stat(abs_path).st_mtime_ns
        except OSError:
            return None
        chain = _file_aliases(abs_path, mtime_ns).get(name)
        if chain is None:
            return None
        (kind, value), fields = chain
        if kind == "import":
            target = self._resolve_import(value, importer=path)
        else:
            target = self._reexport(path, value, depth + 1) or ("decl", path, value)
        for field in fields:
            target = self._member(target, field, depth + 1)
        return target

    def _member(self, target, name: str, depth: int = 0):
        if target is None or target == _UNKNOWN:
            return target
        kind = target[0]
        if kind == "external":
            return target
        if kind == "file":
            if target[1] != self.relative_path:
                reexported = self._reexport(target[1], name, depth)
                if reexported is not None:
                    return reexported
            return ("decl", target[1], name)
        if kind == "decl":
            return ("decl", target[1], self._qualify(target[2], name))
        if kind == "value":
            type_target = target[1]
            if type_target is None:
                return _UNKNOWN
            if type_target == _EXTERNAL:
                return _EXTERNAL
            path, logical = type_target[1], type_target[2]
            container = self._containers.get(logical) if path == self.relative_path else None
            if container is not None and name in container.fields:
                field_type = self._type_target(container.fields[name], logical)
                return ("value", field_type) if field_type is not None else _UNKNOWN
            return _UNKNOWN
        return None

    def _method_of(self, target, name: str):
        """Callee of `<target>.name(...)`."""
        if target is not None and target[0] == "value":
            type_target = target[1]
            if type_target is None:
                return ("method", name)
            if type_target == _EXTERNAL:
                return _EXTERNAL
            return ("decl", type_target[1], self._qualify(type_target[2], name))
        if target == _UNKNOWN:
            return ("method", name)
        return self._member(target, name)

    def _call_result(self, callee):
        if callee == _EXTERNAL:
            return _EXTERNAL
        if callee is None or callee[0] != "decl":
            return _UNKNOWN
        path, logical = callee[1], callee[2]
        if (path == self.relative_path and logical in self._containers) or (
            path != self.relative_path and _is_type_name(logical.rpartition(".")[2])
        ):
            return callee  # a generic type function: `List(u8)` is the type
        if path == self.relative_path and logical in self._returns:
            result = self._function_return(logical)
            if result is not None:
                return ("value", result)
        owner, _, method = logical.rpartition(".")
        if owner and method in _CONSTRUCTOR_NAMES:
            return ("value", ("decl", path, owner))
        return _UNKNOWN

    def _function_return(self, logical: str):
        if logical in self._return_cache:
            return self._return_cache[logical]
        self._return_cache[logical] = None
        type_node, scope = self._returns[logical]
        result = self._type_target(type_node, scope)
        self._return_cache[logical] = result
        return result

    def _type_target(self, type_node, scope: str):
        """The declaration a type expression names (`*Self`, `!?util.T`), or
        `_EXTERNAL` for a std / third-party type."""
        node = type_node
        while node is not None and node.type in ("ParamType", "ErrorUnionExpr"):
            if node.type == "ParamType":
                node = self._child(node, "ErrorUnionExpr")
                continue
            suffixes = [child for child in node.children if child.type == "SuffixExpr"]
            if not suffixes:
                return None
            node = suffixes[-1]
        if node is None or node.type != "SuffixExpr":
            return None
        target = self._eval_chain(node, scope, None, None)
        if target == _EXTERNAL:
            return _EXTERNAL
        return target if target is not None and target[0] == "decl" else None

    def _expr_target(self, holder, scope: str, locals_: dict | None, caller_id: str | None):
        """Target of a `VarDecl` initializer (or of any expression node)."""
        if holder.type == "VarDecl":
            init_list = self._child(holder, "InitList")
            value = self._var_value(holder)
            if init_list is not None:
                return ("value", self._type_target(value, scope)) if value is not None else None
            holder = value
            if holder is None:
                return None
        node = holder
        while node is not None and node.type in ("UnaryExpr", "ErrorUnionExpr"):
            if node.type == "UnaryExpr":
                node = node.child_by_field_name("left")
            else:
                node = self._child(node, "SuffixExpr")
        if node is None or node.type != "SuffixExpr":
            return None
        return self._eval_chain(node, scope, locals_, caller_id)

    def _builtin_target(self, builtin, args, scope: str):
        name = self._text(builtin)
        if name == "@This":
            return ("decl", self.relative_path, scope)
        if name == "@import" and args is not None:
            for literal in self._descendants(args, "STRINGLITERALSINGLE"):
                return self._resolve_import(self._text(literal).strip('"'))
            return _EXTERNAL
        return None

    @staticmethod
    def _descendants(node, node_type: str):
        stack = list(reversed(node.children))
        while stack:
            current = stack.pop()
            if current.type == node_type:
                yield current
            stack.extend(reversed(current.children))

    # ------------------------------------------------------------------
    # Pass 2: relationships
    # ------------------------------------------------------------------

    def _eval_chain(self, suffix, scope: str, locals_: dict | None, caller_id: str | None):
        """Evaluate a `SuffixExpr` (`a.b.c(x)`), emitting call edges for
        `caller_id` when given, and return what the chain evaluates to."""
        children = suffix.children
        if not children:
            return None
        base = children[0]
        rest = children[1:]
        if base.type == "IDENTIFIER":
            target = self._lookup(self._text(base), scope, locals_)
            if rest and rest[0].type == "FnCallArguments":
                # Bare call: only declarations of this file are in scope.
                if target is not None and target[0] == "decl":
                    self._emit(caller_id, target, base)
                target = self._call_result(target)
                rest = rest[1:]
        elif base.type == "BUILTINIDENTIFIER":
            args = rest[0] if rest and rest[0].type == "FnCallArguments" else None
            target = self._builtin_target(base, args, scope)
            if args is not None:
                rest = rest[1:]
        else:
            return None

        for op in rest:
            if op.type != "FieldOrFnCall":
                return None
            call_name = op.child_by_field_name("function_call")
            if call_name is not None:
                callee = self._method_of(target, self._text(call_name))
                self._emit(caller_id, callee, call_name)
                target = self._call_result(callee)
                continue
            field = op.child_by_field_name("field_access")
            if field is None:
                return None
            target = self._member(target, self._text(field))
        return target

    def _emit(self, caller_id: str | None, target, anchor):
        if caller_id is None or target is None:
            return
        line = anchor.start_point[0] + 1
        if target[0] == "method":
            self._add_relationship(caller_id, target[1], line, resolved=False)
            return
        if target[0] != "decl" or not target[2]:
            return
        path, logical = target[1], target[2]
        if path == self.relative_path:
            if logical not in self.top_level_nodes:
                return
            self._add_relationship(caller_id, self._get_component_id(logical), line, True)
        elif self._declared_in(path, logical):
            self._add_relationship(caller_id, f"{path}::{logical}", line, resolved=False)

    def _declared_in(self, path: str, logical: str) -> bool:
        """Whether another repository file declares the component; `extern`
        prototypes and plain constants are not components."""
        if not self.repo_path:
            return True
        abs_path = os.path.join(self.repo_path, path)
        try:
            mtime_ns = os.stat(abs_path).st_mtime_ns
        except OSError:
            return False
        return logical in _file_components(abs_path, mtime_ns, self.repo_path)

    def _add_relationship(self, caller_id: str, callee: str, line: int, resolved: bool):
        if caller_id == callee:
            return
        key = (caller_id, callee)
        if key in self.seen_relationships:
            return
        self.seen_relationships.add(key)
        self.call_relationships.append(
            CallRelationship(caller=caller_id, callee=callee, call_line=line, is_resolved=resolved)
        )

    def _emit_type_refs(self, type_node, scope: str, caller_id: str | None):
        if caller_id is None or type_node is None:
            return
        suffixes = [type_node] if type_node.type == "SuffixExpr" else []
        suffixes.extend(self._descendants(type_node, "SuffixExpr"))
        for suffix in suffixes:
            target = self._eval_chain(suffix, scope, None, caller_id)
            if target is not None and target[0] == "decl":
                self._emit(caller_id, target, suffix)

    def _extract_container_relationships(self, container: _Container):
        scope = container.logical
        container_id = self._get_component_id(scope) if scope in self.top_level_nodes else None
        for type_node in container.fields.values():
            self._emit_type_refs(type_node, scope, container_id)
        for child in container.node.children:
            if child.type != "Decl":
                continue
            proto = self._child(child, "FnProto")
            body = self._child(child, "Block")
            if proto is not None and body is not None:
                name_node = proto.child_by_field_name("function")
                if name_node is None:
                    continue
                logical = self._qualify(scope, self._text(name_node))
                if logical in self._containers:
                    continue  # generic type function: handled as a container
                self._extract_function(proto, body, logical, scope)
            elif container_id is not None:
                var_decl = self._child(child, "VarDecl")
                if var_decl is not None and self._var_value(var_decl) is not None:
                    if self._container_decl(self._var_value(var_decl)) is None:
                        self._walk(var_decl, scope, None, container_id)

    def _extract_function(self, proto, body, logical: str, scope: str):
        caller_id = self._get_component_id(logical)
        locals_: dict = {}
        params = self._child(proto, "ParamDeclList")
        if params is not None:
            for param in params.children:
                if param.type != "ParamDecl":
                    continue
                name = param.child_by_field_name("parameter")
                param_type = self._child(param, "ParamType")
                if name is None:
                    continue
                locals_[self._text(name)] = (
                    self._type_target(param_type, scope) if param_type is not None else None
                )
                if param_type is not None:
                    self._emit_type_refs(param_type, scope, caller_id)
        return_type = self._return_type_node(proto)
        if return_type is not None:
            self._emit_type_refs(return_type, scope, caller_id)
        self._walk(body, scope, locals_, caller_id)

    def _walk(self, node, scope: str, locals_: dict | None, caller_id: str):
        if node.type in ("ContainerDecl", "TestDecl"):
            return  # anonymous containers are not followed
        if node.type == "SuffixExpr":
            self._eval_chain(node, scope, locals_, caller_id)
        elif node.type == "InitList":
            type_expr = node.prev_named_sibling
            if type_expr is not None and type_expr.type == "ErrorUnionExpr":
                target = self._type_target(type_expr, scope)
                if target is not None:
                    self._emit(caller_id, target, node)
        elif node.type == "VarDecl" and locals_ is not None:
            for child in node.children:
                self._walk(child, scope, locals_, caller_id)
            name = node.child_by_field_name("variable_type_function")
            if name is not None:
                type_node = self._var_type_node(node)
                if type_node is not None:
                    self._emit_type_refs(type_node, scope, caller_id)
                    type_target = self._type_target(type_node, scope)
                else:
                    target = self._expr_target(node, scope, locals_, None)
                    if target == _EXTERNAL:
                        type_target = _EXTERNAL
                    elif target is not None and target[0] == "value":
                        type_target = target[1]
                    else:
                        type_target = None
                locals_[self._text(name)] = type_target
            return
        for child in node.children:
            self._walk(child, scope, locals_, caller_id)


def analyze_zig_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterZigAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
