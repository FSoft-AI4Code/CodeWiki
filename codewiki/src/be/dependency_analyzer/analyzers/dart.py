"""Tree-sitter based Dart analyzer.

Extracts classes, mixins, extensions, enums, top-level functions and
getters, and their methods/getters as documentable components, plus
inheritance, field-type, instantiation and call dependency edges.
Import/part handling lives in ``dart_imports.py``; Flutter knowledge
(widgets, state classes, Riverpod, GoRouter) in ``dart_flutter.py``.

Component types follow the Rust/Scala two-level split: ``component_type``
is the coarse type leaf selection gates on ("class", "interface",
"method", "function"), while ``node_type`` and ``display_name`` keep the
Dart/Flutter construct ("mixin", "extension", "enum", "widget", ...).

The grammar comes from tree-sitter-language-pack (ABI 14, compatible with
the pinned tree-sitter 0.23 runtime). It has no call node: a call is a
primary (identifier/this/super) followed by sibling ``selector`` nodes,
the last of which holds an ``argument_part``.

Known limitations:
- Dart 3.3+ ``extension type``, unnamed ``library;`` and null-aware
  collection elements (``?x``) parse as ERROR nodes; declarations outside
  the broken span are still extracted.
- Generic invocations ``Foo<T>(x)`` parse as relational expressions and
  are recovered from the node text.
- Constructors, setters and operators are not components; calls in their
  bodies, in initializer lists and in field initializers are attributed
  to the enclosing class.
"""

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tree_sitter_language_pack import get_parser

from codewiki.src.be.dependency_analyzer.analyzers.dart_flutter import (
    COMPOSITION_METHOD_RE,
    REF_METHODS,
    RIVERPOD_ANNOTATIONS,
    RIVERPOD_NOTIFIER_TYPE,
    RIVERPOD_PROVIDER_TYPE,
    classify_class,
    classify_top_level_initializer,
)
from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
    DartDirective,
    parse_directives,
)
from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node

logger = logging.getLogger(__name__)

_CLASS_MODIFIERS = frozenset({"abstract", "sealed", "base", "interface", "final", "mixin"})
# Member signatures that become components, with their node_type.
_MEMBER_SIGNATURES = {"function_signature": "method", "getter_signature": "getter"}
_TOP_LEVEL_SIGNATURES = {"function_signature": "function", "getter_signature": "getter"}
# Member signatures whose code belongs to the class rather than a component.
_CLASS_LEVEL_SIGNATURES = frozenset(
    {
        "constructor_signature",
        "factory_constructor_signature",
        "constant_constructor_signature",
        "redirecting_factory_constructor_signature",
        "setter_signature",
        "operator_signature",
    }
)

# Dart core and ubiquitous Flutter framework types never emitted as edges.
DART_CORE_TYPES = frozenset(
    {
        "int",
        "double",
        "num",
        "bool",
        "String",
        "Object",
        "dynamic",
        "void",
        "Never",
        "Null",
        "Function",
        "Type",
        "Symbol",
        "Record",
        "Enum",
        "Comparable",
        "BigInt",
        "List",
        "Map",
        "Set",
        "Iterable",
        "Iterator",
        "MapEntry",
        "Future",
        "FutureOr",
        "Stream",
        "StreamController",
        "StreamSubscription",
        "Completer",
        "Timer",
        "Zone",
        "Duration",
        "DateTime",
        "Uri",
        "RegExp",
        "StringBuffer",
        "StackTrace",
        "Sink",
        "Uint8List",
        "Exception",
        "Error",
        "StateError",
        "ArgumentError",
        "RangeError",
        "FormatException",
        "UnimplementedError",
        "UnsupportedError",
        "Widget",
        "BuildContext",
        "Key",
        "ValueKey",
        "GlobalKey",
        "UniqueKey",
        "ObjectKey",
        "State",
        "StatelessWidget",
        "StatefulWidget",
        "WidgetRef",
        "Ref",
    }
)
# Bare calls to SDK/framework functions that never point at a repo component.
DART_NOISE_CALLS = frozenset(
    {
        "print",
        "debugPrint",
        "identical",
        "setState",
        "jsonEncode",
        "jsonDecode",
        "unawaited",
        "max",
        "min",
        "runApp",
        "assert",
        "scheduleMicrotask",
    }
)
_GENERIC_CALL_RE = re.compile(r"\A\s*([A-Za-z_$][\w$]*)\s*<([^()]*)>\s*\(", re.S)
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")

# The bundled tree-sitter Dart grammar predates Dart 3.8 null-aware collection
# elements (`[?a]`, `{'k': ?v}`, `{?x}`) and mis-parses everything after them.
# A `?` directly after `[`, `{`, `,` or `:` (never a ternary/nullable/`?.`) is
# blanked with a space before parsing, so byte/line/column positions are kept.
_NULL_AWARE_ELEMENT_RE = re.compile(r"([\[{,:]\s*)\?(?=[A-Za-z_$(])")


def _blank_null_aware_elements(source: str) -> str:
    return _NULL_AWARE_ELEMENT_RE.sub(lambda m: m.group(1) + " ", source)


def _is_type_name(name: str) -> bool:
    stripped = name.lstrip("_$")
    return bool(stripped) and stripped[0].isupper()


def _selector_tokens(selectors) -> list[tuple[str, str | None, object]]:
    """Flatten a selector chain into ("member", name, node) / ("call", None,
    argument_part) / ("other", None, node) tokens."""
    tokens: list[tuple[str, str | None, object]] = []
    for selector in selectors:
        for child in selector.named_children:
            if child.type in (
                "unconditional_assignable_selector",
                "conditional_assignable_selector",
            ):
                ident = _first_child(child, "identifier")
                tokens.append(("member", _text(ident) if ident is not None else None, child))
            elif child.type == "argument_part":
                tokens.append(("call", None, child))
            else:
                tokens.append(("other", None, child))
    return tokens


def _local_declared_types(*roots) -> dict[str, str]:
    """name -> declared type for parameters and typed locals under ``roots``."""
    types: dict[str, str] = {}
    for root in roots:
        if root is None:
            continue
        for node in [root, *_descendants(root)]:
            if node.type not in ("formal_parameter", "initialized_variable_definition"):
                continue
            declared = _first_child(node, "type_identifier")
            ident = node.child_by_field_name("name")
            if declared is not None and ident is not None:
                types[_text(ident)] = _text(declared)
    return types


@lru_cache(maxsize=1)
def _dart_parser():
    return get_parser("dart")


def _text(node) -> str:
    return node.text.decode("utf8", errors="replace") if node is not None else ""


def _has_riverpod_annotation(annotations) -> bool:
    return any(_text(a.child_by_field_name("name")) in RIVERPOD_ANNOTATIONS for a in annotations)


def _first_child(node, *types):
    return next((c for c in node.named_children if c.type in types), None) if node else None


def _class_kind(modifiers: set[str]) -> tuple[str, str]:
    if "interface" in modifiers and "abstract" in modifiers:
        return "interface", "interface"
    if "sealed" in modifiers:
        return "class", "sealed class"
    if "abstract" in modifiers:
        return "class", "abstract class"
    if "mixin" in modifiers:
        return "class", "mixin class"
    if "interface" in modifiers:
        return "class", "interface class"
    return "class", "class"


def _member_signature(wrapper):
    if wrapper.type in _MEMBER_SIGNATURES or wrapper.type in _CLASS_LEVEL_SIGNATURES:
        return wrapper
    return _first_child(wrapper, *_MEMBER_SIGNATURES, *_CLASS_LEVEL_SIGNATURES)


def _parameter_names(signature) -> list[str] | None:
    params = _first_child(signature, "formal_parameter_list")
    if params is None:
        return None
    names: list[str] = []
    stack = [params]
    while stack:
        current = stack.pop()
        if current.type == "formal_parameter":
            ident = current.child_by_field_name("name")
            if ident is None:
                idents = [n for n in _descendants(current) if n.type == "identifier"]
                ident = idents[-1] if idents else None
            if ident is not None:
                names.append(_text(ident))
            continue
        stack.extend(reversed(current.named_children))
    return names


def _descendants(node):
    stack = list(reversed(node.named_children))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


@dataclass
class _ScanTarget:
    caller: str
    owner: str | None
    node: object
    signature: object | None = None
    method_name: str | None = None


class TreeSitterDartAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        self.directives: list[DartDirective] = []
        self.import_prefixes: set[str] = set()
        # Same-file symbol table keyed by logical name ("Foo", "Foo.bar").
        self.top_level_nodes: dict[str, Node] = {}
        self.seen_relationships: set = set()
        self._scan_targets: list[_ScanTarget] = []
        self._field_types: dict[str, dict[str, str]] = {}
        self._field_decls: list[tuple[str, object]] = []
        self._type_ref_nodes: dict[str, list] = {}
        self._class_extends: dict[str, str | None] = {}
        self._type_params: set[str] = set()
        self._extension_on: dict[str, str] = {}
        self._top_level_vars: list[tuple[str, object]] = []
        self._analyze()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @property
    def relative_path(self) -> str:
        if self.repo_path:
            try:
                return Path(os.path.relpath(str(self.file_path), self.repo_path)).as_posix()
            except ValueError:
                pass
        return self.file_path.as_posix()

    def _component_id(self, logical_name: str) -> str:
        return f"{self.relative_path}::{logical_name}"

    def _analyze(self):
        try:
            tree = _dart_parser().parse(bytes(_blank_null_aware_elements(self.content), "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()
            self.directives = parse_directives(root)
            self.import_prefixes = {
                d.prefix for d in self.directives if d.kind == "import" and d.prefix
            }
            self._type_params = {
                _text(t)
                for n in _descendants(root)
                if n.type == "type_parameter"
                for t in n.named_children[:1]
            }
            self._extract_declarations(root, lines)
            self._apply_flutter_kinds(root, lines)
            self._extract_relationships()
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Dart file {self.file_path}: {e}")

    # ------------------------------------------------------------------
    # Pass 1: declarations
    # ------------------------------------------------------------------

    def _extract_declarations(self, root, lines):
        children = root.named_children
        for i, child in enumerate(children):
            following = children[i + 1] if i + 1 < len(children) else None
            body = (
                following if following is not None and following.type == "function_body" else None
            )
            if child.type == "class_definition":
                self._add_class(child, lines)
            elif child.type == "mixin_declaration":
                self._add_mixin(child, lines)
            elif child.type == "extension_declaration":
                self._add_extension(child, lines)
            elif child.type == "enum_declaration":
                self._add_enum(child, lines)
            elif child.type in _TOP_LEVEL_SIGNATURES:
                self._add_top_level_function(child, body, lines)
            elif child.type == "static_final_declaration_list":
                for declaration in child.named_children:
                    if declaration.type == "static_final_declaration":
                        name = _first_child(declaration, "identifier")
                        if name is not None:
                            self._top_level_vars.append((_text(name), declaration))

    def _add_class(self, node, lines):
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        modifiers = {c.type for c in node.children} & _CLASS_MODIFIERS
        component_type, node_type = _class_kind(modifiers)
        superclass = node.child_by_field_name("superclass")
        interfaces = node.child_by_field_name("interfaces")
        extends = None
        mixins: list[str] = []
        if superclass is not None:
            first = _first_child(superclass, "type_identifier")
            extends = _text(first) if first is not None else None
        for holder in (superclass, node):
            mixin_node = _first_child(holder, "mixins")
            if mixin_node is not None:
                mixins = [
                    _text(t) for t in mixin_node.named_children if t.type == "type_identifier"
                ]
                break
        implemented = (
            [_text(t) for t in interfaces.named_children if t.type == "type_identifier"]
            if interfaces is not None
            else []
        )
        bases = [b for b in (extends, *mixins, *implemented) if b]
        self._register_type(
            node, name, component_type, node_type, lines, bases, [superclass, interfaces]
        )
        self._class_extends[name] = extends
        self._extract_members(name, node.child_by_field_name("body"), lines)

    def _add_mixin(self, node, lines):
        name = _text(_first_child(node, "identifier"))
        if not name:
            return
        on_types = [_text(c) for c in node.named_children if c.type == "type_identifier"]
        interfaces = _first_child(node, "interfaces")
        implemented = (
            [_text(t) for t in interfaces.named_children if t.type == "type_identifier"]
            if interfaces is not None
            else []
        )
        on_nodes = [c for c in node.named_children if c.type == "type_identifier"]
        self._register_type(
            node, name, "class", "mixin", lines, on_types + implemented, [*on_nodes, interfaces]
        )
        self._class_extends[name] = None
        self._extract_members(name, _first_child(node, "class_body"), lines)

    def _add_extension(self, node, lines):
        on_type = node.child_by_field_name("class")
        on_name = _text(_first_child(on_type, "type_identifier") or on_type) if on_type else ""
        name = _text(node.child_by_field_name("name"))
        if not name:
            name = f"extension_on_{on_name or 'unknown'}"
            if name in self.top_level_nodes:
                # A second unnamed extension on the same type must not
                # overwrite the first.
                name = f"{name}_L{node.start_point[0] + 1}"
        self._extension_on[name] = on_name
        self._register_type(
            node, name, "class", "extension", lines, [on_name] if on_name else [], [on_type]
        )
        self._class_extends[name] = None
        self._extract_members(name, node.child_by_field_name("body"), lines)

    def _add_enum(self, node, lines):
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        interfaces = _first_child(node, "interfaces")
        mixin_node = _first_child(node, "mixins")
        bases = [
            _text(t)
            for holder in (mixin_node, interfaces)
            if holder is not None
            for t in holder.named_children
            if t.type == "type_identifier"
        ]
        self._register_type(node, name, "class", "enum", lines, bases, [mixin_node, interfaces])
        self._class_extends[name] = None
        self._extract_members(name, node.child_by_field_name("body"), lines)

    def _register_type(self, node, name, component_type, node_type, lines, bases, type_nodes):
        self._add_node(
            node, node, name, component_type, node_type, lines, base_classes=bases or None
        )
        self._type_ref_nodes[name] = [n for n in type_nodes if n is not None]

    def _extract_members(self, owner: str, body, lines):
        if body is None:
            return
        children = body.named_children
        for i, child in enumerate(children):
            following = children[i + 1] if i + 1 < len(children) else None
            body_node = (
                following if following is not None and following.type == "function_body" else None
            )
            if child.type == "method_signature":
                self._add_member(owner, child, body_node, lines)
            elif child.type == "declaration":
                if _member_signature(child) is not None:
                    self._add_member(owner, child, body_node, lines)
                else:
                    self._add_field(owner, child)

    def _add_member(self, owner: str, wrapper, body, lines):
        signature = _member_signature(wrapper)
        if signature is None:
            return
        if signature.type in _MEMBER_SIGNATURES:
            name = _text(signature.child_by_field_name("name"))
            if not name:
                return
            node = self._add_node(
                wrapper,
                body or wrapper,
                f"{owner}.{name}",
                "method",
                _MEMBER_SIGNATURES[signature.type],
                lines,
                class_name=owner,
                parameters=_parameter_names(signature),
            )
            if body is not None:
                self._scan_targets.append(_ScanTarget(node.id, owner, body, signature, name))
            return
        # Constructors (incl. initializer lists), setters, operators: the
        # class owns their code.
        class_id = self._component_id(owner)
        self._scan_targets.append(_ScanTarget(class_id, owner, wrapper, signature))
        if body is not None:
            self._scan_targets.append(_ScanTarget(class_id, owner, body, signature))

    def _add_field(self, owner: str, declaration):
        declared = _first_child(declaration, "type_identifier")
        names = [
            _text(_first_child(item, "identifier"))
            for holder in declaration.named_children
            if holder.type in ("initialized_identifier_list", "static_final_declaration_list")
            for item in holder.named_children
            if item.type in ("initialized_identifier", "static_final_declaration")
        ]
        if declared is not None:
            fields = self._field_types.setdefault(owner, {})
            for name in names:
                if name:
                    fields[name] = _text(declared)
        self._field_decls.append((owner, declaration))
        self._scan_targets.append(_ScanTarget(self._component_id(owner), owner, declaration))

    def _add_top_level_function(self, signature, body, lines):
        name = _text(signature.child_by_field_name("name"))
        if not name:
            return
        node = self._add_node(
            signature,
            body or signature,
            name,
            "function",
            _TOP_LEVEL_SIGNATURES[signature.type],
            lines,
            parameters=_parameter_names(signature),
        )
        if body is not None:
            self._scan_targets.append(_ScanTarget(node.id, None, body, signature, name))

    # ------------------------------------------------------------------
    # Node construction
    # ------------------------------------------------------------------

    @staticmethod
    def _docstring(anchor) -> str:
        comments: list[str] = []
        sibling = anchor.prev_named_sibling
        while sibling is not None and sibling.type in (
            "annotation",
            "documentation_comment",
            "comment",
        ):
            if sibling.type != "annotation":
                text = _text(sibling).strip()
                if sibling.type == "comment" and not text.startswith(("///", "/**")):
                    break
                comments.append(text)
            sibling = sibling.prev_named_sibling
        return "\n".join(reversed(comments))

    def _add_node(
        self,
        start,
        end,
        logical_name: str,
        component_type: str,
        node_type: str,
        lines,
        class_name: str | None = None,
        parameters: list[str] | None = None,
        base_classes: list[str] | None = None,
    ) -> Node:
        component_id = self._component_id(logical_name)
        start_idx = start.start_point[0]
        end_idx = end.end_point[0] + 1
        # Annotations of members and top-level functions are preceding
        # siblings; include them in the span (class annotations are children).
        sibling = start.prev_named_sibling
        while sibling is not None and sibling.type == "annotation":
            start_idx = min(start_idx, sibling.start_point[0])
            sibling = sibling.prev_named_sibling
        docstring = self._docstring(start)
        node = Node(
            id=component_id,
            name=logical_name,
            component_type=component_type,
            file_path=str(self.file_path),
            relative_path=self.relative_path,
            source_code="\n".join(lines[start_idx:end_idx]) if start_idx < len(lines) else "",
            start_line=start_idx + 1,
            end_line=end_idx,
            has_docstring=bool(docstring),
            docstring=docstring,
            parameters=parameters,
            node_type=node_type,
            base_classes=base_classes,
            class_name=class_name,
            display_name=f"{node_type} {logical_name}",
            component_id=component_id,
            language="dart",
        )
        self.nodes.append(node)
        self.top_level_nodes[logical_name] = node
        return node

    # ------------------------------------------------------------------
    # Flutter / Riverpod
    # ------------------------------------------------------------------

    def _apply_flutter_kinds(self, root, lines):
        # Same-file widget subclass chains: iterate to a fixpoint.
        widgets: set[str] = set()
        changed = True
        while changed:
            changed = False
            for name, extends in self._class_extends.items():
                if name not in widgets and classify_class(extends, widgets) == "widget":
                    widgets.add(name)
                    changed = True
        for name, extends in self._class_extends.items():
            kind = classify_class(extends, widgets)
            if kind:
                self._set_node_type(name, kind)

        # @riverpod classes (annotation is a child) and functions (preceding sibling).
        for child in root.named_children:
            if child.type == "class_definition":
                annotations = [c for c in child.named_children if c.type == "annotation"]
                if _has_riverpod_annotation(annotations):
                    self._set_node_type(
                        _text(child.child_by_field_name("name")), RIVERPOD_NOTIFIER_TYPE
                    )
            elif child.type in _TOP_LEVEL_SIGNATURES:
                annotations = []
                sibling = child.prev_named_sibling
                while sibling is not None and sibling.type in (
                    "annotation",
                    "documentation_comment",
                    "comment",
                ):
                    if sibling.type == "annotation":
                        annotations.append(sibling)
                    sibling = sibling.prev_named_sibling
                if _has_riverpod_annotation(annotations):
                    self._set_node_type(
                        _text(child.child_by_field_name("name")), RIVERPOD_PROVIDER_TYPE
                    )

        # Top-level provider / router variables become components.
        for name, declaration in self._top_level_vars:
            text = _text(declaration)
            initializer = text.split("=", 1)[1] if "=" in text else ""
            kind = classify_top_level_initializer(initializer)
            if kind:
                node = self._add_node(declaration, declaration, name, "function", kind, lines)
                self._scan_targets.append(_ScanTarget(node.id, None, declaration))

    def _set_node_type(self, name: str, node_type: str):
        node = self.top_level_nodes.get(name)
        if node is not None and node.component_type in ("class", "interface", "function"):
            node.node_type = node_type
            node.display_name = f"{node_type} {name}"

    # ------------------------------------------------------------------
    # Pass 2: relationships
    # ------------------------------------------------------------------

    def _extract_relationships(self):
        for name, type_nodes in self._type_ref_nodes.items():
            caller = self._component_id(name)
            for type_node in type_nodes:
                line = type_node.start_point[0] + 1
                for ref in [type_node, *_descendants(type_node)]:
                    if ref.type == "type_identifier":
                        self._add_type_edge(caller, _text(ref), line)
        for owner, declaration in self._field_decls:
            caller = self._component_id(owner)
            line = declaration.start_point[0] + 1
            for child in declaration.named_children:
                if child.type == "type_identifier":
                    self._add_type_edge(caller, _text(child), line)
                elif child.type == "type_arguments":
                    for ref in _descendants(child):
                        if ref.type == "type_identifier":
                            self._add_type_edge(caller, _text(ref), line)
        for target in self._scan_targets:
            self._scan(target, self._lift_target(target))

    def _lift_target(self, target) -> str | None:
        """Instantiations in a widget's/state's build helpers and createState
        are also recorded on the class: the widget tree is class-level."""
        if not target.owner or not target.method_name:
            return None
        owner = self.top_level_nodes.get(target.owner)
        if owner is None or owner.node_type not in ("widget", "state"):
            return None
        if not COMPOSITION_METHOD_RE.match(target.method_name):
            return None
        return owner.id

    def _scan(self, target, lift_to: str | None = None):
        local_types = _local_declared_types(target.signature, target.node)
        stack = [target.node]
        while stack:
            node = stack.pop()
            if node.type in ("const_object_expression", "new_expression"):
                type_node = _first_child(node, "type_identifier")
                if type_node is not None:
                    self._add_instantiation(target.caller, _text(type_node), node, lift_to)
            elif node.type == "ERROR":
                self._handle_error_call(target, node)
            elif node.type == "relational_expression":
                self._handle_generic_invocation(target, node, lift_to)
            children = node.children
            i = 0
            while i < len(children):
                child = children[i]
                if child.type in ("identifier", "this", "super"):
                    j = i + 1
                    selectors = []
                    while j < len(children) and children[j].type == "selector":
                        selectors.append(children[j])
                        j += 1
                    cascades = []
                    while j < len(children) and children[j].type == "cascade_section":
                        cascades.append(children[j])
                        j += 1
                    if selectors:
                        self._process_chain(target, child, selectors, local_types, lift_to)
                    elif cascades and child.type == "identifier":
                        for cascade in cascades:
                            self._process_cascade(target, child, cascade, local_types)
                    i = j if j > i + 1 else i + 1
                    continue
                i += 1
            stack.extend(reversed(node.named_children))

    def _process_chain(self, target, head, selectors, local_types, lift_to):
        tokens = _selector_tokens(selectors)
        if not tokens:
            return
        caller, owner = target.caller, target.owner
        line = head.start_point[0] + 1
        name = _text(head)
        if head.type == "identifier" and name in self.import_prefixes and tokens[0][0] == "member":
            name, tokens = tokens[0][1] or "", tokens[1:]
            if not tokens or not name:
                return
        kind = tokens[0][0]
        called_member = kind == "member" and len(tokens) > 1 and tokens[1][0] == "call"

        if head.type in ("this", "super"):
            if head.type == "this" and called_member and owner:
                local = self.top_level_nodes.get(f"{owner}.{tokens[0][1]}")
                if local is not None:
                    self._add_resolved(caller, local.id, line)
            elif (
                head.type == "this"
                and owner
                and len(tokens) == 3
                and tokens[0][0] == "member"
                and tokens[1][0] == "member"
                and tokens[2][0] == "call"
            ):
                receiver = self._field_types.get(owner, {}).get(tokens[0][1] or "")
                if receiver and tokens[1][1]:
                    self._add_member_call_on_type(caller, receiver, tokens[1][1], line)
            return

        if _is_type_name(name):
            if kind == "call":
                self._add_instantiation(caller, name, head, lift_to)
            elif kind == "member":
                local = self.top_level_nodes.get(f"{name}.{tokens[0][1]}")
                if called_member and local is not None:
                    self._add_resolved(caller, local.id, line)
                elif called_member:
                    # Named constructor or static method: depends on the type.
                    self._add_instantiation(caller, name, head, lift_to)
                else:
                    # Enum value, static field, constructor tear-off.
                    self._add_type_edge(caller, name, line)
            return

        if kind == "call":
            self._add_function_call(caller, owner, name, line)
            return
        if called_member:
            member = tokens[0][1] or ""
            if self._handle_ref_call(caller, member, tokens[1][2], line):
                return
            receiver = local_types.get(name) or self._field_types.get(owner or "", {}).get(name)
            if receiver:
                self._add_member_call_on_type(caller, receiver, member, line)

    def _process_cascade(self, target, head, cascade, local_types):
        selector = _first_child(cascade, "cascade_selector")
        ident = _first_child(selector, "identifier") if selector is not None else None
        if ident is None or _first_child(cascade, "argument_part") is None:
            return
        name = _text(head)
        receiver = local_types.get(name) or self._field_types.get(target.owner or "", {}).get(name)
        if receiver:
            self._add_member_call_on_type(
                target.caller, receiver, _text(ident), head.start_point[0] + 1
            )

    def _handle_error_call(self, target, node):
        """Calls to contextual keywords (``get()``, ``set()``) parse as an
        ERROR holding a function_signature; recover them as bare calls."""
        signature = _first_child(node, "function_signature")
        if signature is None or signature.start_byte != node.start_byte:
            return
        name = signature.child_by_field_name("name")
        if name is not None and _first_child(signature, "formal_parameter_list") is not None:
            self._add_function_call(
                target.caller, target.owner, _text(name), node.start_point[0] + 1
            )

    def _handle_generic_invocation(self, target, node, lift_to):
        named = node.named_children
        if (
            len(named) < 2
            or named[0].type != "relational_expression"
            or named[-1].type != "parenthesized_expression"
        ):
            return
        match = _GENERIC_CALL_RE.match(_text(node))
        if not match:
            return
        name, type_args = match.group(1), match.group(2)
        line = node.start_point[0] + 1
        if _is_type_name(name):
            self._add_instantiation(target.caller, name, node, lift_to)
        else:
            self._add_function_call(target.caller, target.owner, name, line)
        for arg in _IDENT_RE.findall(type_args):
            if _is_type_name(arg):
                self._add_type_edge(target.caller, arg, line)

    def _handle_ref_call(self, caller: str, member: str, call_node, line: int) -> bool:
        """`ref.watch(fooProvider)` / `.read` / `.listen` / ...: an edge to the
        provider (`.notifier`, `.future` and family arguments stripped)."""
        if member not in REF_METHODS:
            return False
        arguments = _first_child(call_node, "arguments")
        first = (
            arguments.named_children[0]
            if arguments is not None and arguments.named_children
            else None
        )
        if first is not None and first.type == "argument":
            first = first.named_children[0] if first.named_children else None
        if first is None or first.type != "identifier":
            return False
        provider = _text(first)
        if not provider.endswith("Provider"):
            return False
        local = self.top_level_nodes.get(provider)
        if local is not None:
            self._add_resolved(caller, local.id, line)
        else:
            self._add_raw(caller, provider, line)
        return True

    # ------------------------------------------------------------------
    # Edge emission
    # ------------------------------------------------------------------

    def _add_instantiation(self, caller: str, type_name: str, node, lift_to: str | None):
        line = node.start_point[0] + 1
        self._add_type_edge(caller, type_name, line)
        if lift_to:
            self._add_type_edge(lift_to, type_name, line)

    def _add_type_edge(self, caller: str, type_name: str, line: int):
        if not type_name or type_name in DART_CORE_TYPES or type_name in self._type_params:
            return
        local = self.top_level_nodes.get(type_name)
        if local is not None and local.component_type != "method":
            self._add_resolved(caller, local.id, line)
        else:
            self._add_raw(caller, type_name, line)

    def _add_function_call(self, caller: str, owner: str | None, name: str, line: int):
        if not name or name in DART_NOISE_CALLS:
            return
        if owner:
            local = self.top_level_nodes.get(f"{owner}.{name}")
            if local is not None:
                self._add_resolved(caller, local.id, line)
                return
        local = self.top_level_nodes.get(name)
        if local is not None and local.component_type == "function":
            self._add_resolved(caller, local.id, line)
            return
        on_type = self._extension_on.get(owner or "")
        if on_type is not None:
            # A bare call inside an extension may target the extended type
            # or a free function; a core extended type means neither is ours.
            if on_type and on_type not in DART_CORE_TYPES and on_type not in self._type_params:
                self._add_raw(caller, f"{on_type}.{name}", line)
                self._add_raw(caller, name, line)
            return
        self._add_raw(caller, name, line)

    def _add_member_call_on_type(self, caller: str, type_name: str, member: str, line: int):
        if not member or type_name in DART_CORE_TYPES or type_name in self._type_params:
            return
        local = self.top_level_nodes.get(f"{type_name}.{member}")
        if local is not None:
            self._add_resolved(caller, local.id, line)
        else:
            self._add_raw(caller, f"{type_name}.{member}", line)

    def _add_resolved(self, caller: str, callee_id: str, line: int):
        self._add_relationship(caller, callee_id, line, True)

    def _add_raw(self, caller: str, callee: str, line: int):
        self._add_relationship(caller, callee, line, False)

    def _add_relationship(self, caller: str, callee: str, line: int | None, resolved: bool):
        key = (caller, callee, line)
        if caller == callee or key in self.seen_relationships:
            return
        self.seen_relationships.add(key)
        self.call_relationships.append(
            CallRelationship(caller=caller, callee=callee, call_line=line, is_resolved=resolved)
        )


def analyze_dart_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterDartAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
