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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tree_sitter_language_pack import get_parser

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


@lru_cache(maxsize=1)
def _dart_parser():
    return get_parser("dart")


def _text(node) -> str:
    return node.text.decode("utf8", errors="replace") if node is not None else ""


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
            tree = _dart_parser().parse(bytes(self.content, "utf8"))
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
        name = _text(node.child_by_field_name("name")) or f"extension_on_{on_name or 'unknown'}"
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


def analyze_dart_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterDartAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
