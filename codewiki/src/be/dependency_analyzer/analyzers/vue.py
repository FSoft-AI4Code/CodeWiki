"""Tree-sitter based analyzer for Vue single-file components (`.vue`).

A `.vue` file holds a `<template>`, one or two `<script>` blocks (`<script>`
and `<script setup>`), and styles. The analyzer works in three layers:

1. The script blocks are cut out of the file with everything else blanked
   to spaces (newlines kept), so line numbers stay those of the `.vue` file,
   and handed to the existing JavaScript or TypeScript analyzer
   (`lang="ts"`/`"tsx"` picks TypeScript). Their components keep ids under
   the `.vue` path, e.g. `src/App.vue::useAuth`.
2. The file itself becomes a component, `<path>::<Name>`, where `Name` is the
   PascalCase file stem (the parent directory for `index.vue`). Its
   options-API members (`methods`, `computed`, `watch`, and lifecycle hooks
   such as `created` or `setup`) become methods, keyed `Name.member`.
3. Edges go from the component to what it imports from the repository
   (another `.vue` file resolves to that file's component; `@/` maps to
   `src/`), to the components its template uses (`<article-preview>` and
   `<ArticlePreview>` alike; unknown tags fall back to a bare name, which
   resolves to a globally registered component when exactly one has that
   name), and to the members and script functions its template expressions
   call or bind. Members get edges for `this.member(...)` calls and for calls
   of the file's script functions and imported names.

Known limitations:
- Imports are resolved for relative paths and the `@/` alias only; other
  bundler aliases and package imports are dropped.
- Template expressions are scanned for identifiers, not parsed; a name only
  becomes an edge when the file's script defines it.
- Mixins, `extends`, and `Vue.component(...)` registrations are not followed
  beyond the bare-name fallback for template tags.
"""

import logging
import os
import posixpath
import re
from functools import lru_cache
from pathlib import Path

from tree_sitter_language_pack import get_parser

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node

logger = logging.getLogger(__name__)

# Options-API keys whose entries are member functions of the component.
_MEMBER_GROUPS = {"methods", "computed", "watch"}
# Options-API functions declared directly on the component object.
_LIFECYCLE_HOOKS = {
    "setup",
    "beforeCreate",
    "created",
    "beforeMount",
    "mounted",
    "beforeUpdate",
    "updated",
    "beforeUnmount",
    "unmounted",
    "beforeDestroy",
    "destroyed",
    "activated",
    "deactivated",
    "errorCaptured",
    "beforeRouteEnter",
    "beforeRouteUpdate",
    "beforeRouteLeave",
    "asyncData",
    "fetch",
    "serverPrefetch",
}
# Built-in tags that are never user components.
_BUILTIN_TAGS = {
    "component",
    "slot",
    "template",
    "transition",
    "transition-group",
    "keep-alive",
    "teleport",
    "suspense",
    "router-link",
    "router-view",
    "client-only",
    "nuxt-link",
    "nuxt",
}
_SCRIPT_EXTS = (".vue", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_FUNCTION_VALUES = {"function_expression", "function", "arrow_function", "generator_function"}
_IDENT_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)")
_JS_KEYWORDS = {
    "true",
    "false",
    "null",
    "undefined",
    "this",
    "in",
    "of",
    "new",
    "typeof",
    "instanceof",
    "void",
    "return",
    "if",
    "else",
}


@lru_cache(maxsize=None)
def _parser(language: str):
    return get_parser(language)


def _is_sfc(target: str) -> bool:
    """An import of another `.vue` file names its component exactly; other
    targets are left for CallGraphAnalyzer to resolve (or drop)."""
    return ".vue::" in target


def pascal_case(name: str) -> str:
    """`article-preview` / `article_preview` / `articlePreview` -> `ArticlePreview`."""
    parts = [p for p in re.split(r"[-_.\s]+", name) if p]
    return "".join(p[0].upper() + p[1:] for p in parts)


def vue_component_name(relative_path: str) -> str:
    """Logical component name of a `.vue` file: its PascalCase stem, or the
    parent directory's for `index.vue`."""
    path = Path(relative_path)
    stem = path.stem
    if stem.lower() == "index" and path.parent.name:
        stem = path.parent.name
    return pascal_case(stem) or stem


class TreeSitterVueAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        self.seen_relationships: set = set()
        self.relative_path = Path(self._get_relative_path()).as_posix()
        self.component_name = vue_component_name(self.relative_path)
        self.component_id = f"{self.relative_path}::{self.component_name}"
        # Local name -> exact component id (or bare name) it refers to.
        self.imports: dict[str, str] = {}
        self.import_lines: dict[str, int] = {}
        # Member name -> component id.
        self.members: dict[str, str] = {}
        # Script-level names defined in this file -> component id.
        self.script_names: dict[str, str] = {}
        # Member component id -> its function node, for pass-2 call edges.
        self._member_nodes: dict = {}
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

    @staticmethod
    def _text(node) -> str:
        return node.text.decode("utf8", errors="replace") if node is not None else ""

    def _analyze(self):
        try:
            data = self.content.encode("utf8")
            root = _parser("vue").parse(data).root_node
            scripts, template = self._split_blocks(root)
            lines = self.content.splitlines()

            self._add_component_node(lines)
            if scripts:
                masked = self._mask_outside(data, [(s.start_byte, s.end_byte) for s, _ in scripts])
                use_ts = any(lang in {"ts", "tsx"} for _, lang in scripts)
                self._run_script_analyzer(masked, use_ts)
                script_root = _parser("typescript" if use_ts else "javascript").parse(masked)
                self._collect_imports(script_root.root_node)
                self._extract_options(script_root.root_node, lines)
                self._extract_member_calls()
                for local, target in self.imports.items():
                    line = self.import_lines.get(local, 1)
                    self._add_relationship(self.component_id, target, line, _is_sfc(target))
            if template is not None:
                self._extract_template_edges(template)
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Vue file {self.file_path}: {e}")

    def _split_blocks(self, root):
        """`[(raw_text node, lang)]` for the script blocks, and the template."""
        scripts = []
        template = None
        for child in root.children:
            if child.type == "template_element" and template is None:
                template = child
            elif child.type == "script_element":
                raw = next((c for c in child.children if c.type == "raw_text"), None)
                if raw is not None:
                    scripts.append((raw, self._script_lang(child)))
        return scripts, template

    def _script_lang(self, script_element) -> str:
        start = next((c for c in script_element.children if c.type == "start_tag"), None)
        for attr in start.children if start is not None else []:
            if attr.type != "attribute":
                continue
            name = next((c for c in attr.children if c.type == "attribute_name"), None)
            if self._text(name) != "lang":
                continue
            value = next(
                (
                    c
                    for c in attr.children
                    if c.type in ("quoted_attribute_value", "attribute_value")
                ),
                None,
            )
            return self._text(value).strip("\"' ").lower()
        return "js"

    @staticmethod
    def _mask_outside(data: bytes, keep: list[tuple[int, int]]) -> bytes:
        """Blank every byte outside `keep` to a space, keeping newlines, so the
        script blocks parse at their original byte offsets and lines."""
        masked = bytearray(b if b == 0x0A else 0x20 for b in data)
        for start, end in keep:
            masked[start:end] = data[start:end]
        return bytes(masked)

    # ------------------------------------------------------------------
    # Script blocks
    # ------------------------------------------------------------------

    def _run_script_analyzer(self, masked: bytes, use_ts: bool):
        text = masked.decode("utf8", errors="replace")
        if use_ts:
            from codewiki.src.be.dependency_analyzer.analyzers.typescript import (
                analyze_typescript_file_treesitter as analyze,
            )
        else:
            from codewiki.src.be.dependency_analyzer.analyzers.javascript import (
                analyze_javascript_file_treesitter as analyze,
            )
        nodes, relationships = analyze(str(self.file_path), text, repo_path=self.repo_path)
        for node in nodes:
            if node.id == self.component_id:
                continue
            self.nodes.append(node)
            self.script_names.setdefault(node.name.split(".")[-1], node.id)
        for rel in relationships:
            if rel.caller != self.component_id:
                self.call_relationships.append(rel)

    def _collect_imports(self, root):
        for stmt in root.children:
            if stmt.type != "import_statement":
                continue
            source = stmt.child_by_field_name("source")
            target_file = self._resolve_import_path(self._text(source).strip("\"'`"))
            if target_file is None:
                continue
            clause = next((c for c in stmt.children if c.type == "import_clause"), None)
            if clause is None:
                continue
            for part in clause.children:
                if part.type == "identifier":  # default import
                    local = self._text(part)
                    if target_file.endswith(".vue"):
                        target = f"{target_file}::{vue_component_name(target_file)}"
                    else:
                        target = local
                    self.imports[local] = target
                    self.import_lines[local] = stmt.start_point[0] + 1
                elif part.type == "named_imports":
                    for spec in part.children:
                        if spec.type != "import_specifier":
                            continue
                        name = self._text(spec.child_by_field_name("name"))
                        alias = spec.child_by_field_name("alias")
                        local = self._text(alias) if alias is not None else name
                        self.imports[local] = f"{target_file}::{name}"
                        self.import_lines[local] = stmt.start_point[0] + 1

    def _resolve_import_path(self, spec: str) -> str | None:
        """Repository-relative file an import specifier names, or None when it
        is a package (or points outside the repository)."""
        if spec.startswith("@/"):
            base = posixpath.normpath(posixpath.join("src", spec[2:]))
        elif spec.startswith("."):
            here = posixpath.dirname(self.relative_path)
            base = posixpath.normpath(posixpath.join(here, spec))
        else:
            return None
        if base.startswith(".."):
            return None
        candidates = [base] if base.endswith(_SCRIPT_EXTS) else []
        candidates += [base + ext for ext in _SCRIPT_EXTS]
        candidates += [posixpath.join(base, "index" + ext) for ext in _SCRIPT_EXTS]
        if not self.repo_path:
            return candidates[0]
        for candidate in candidates:
            if os.path.isfile(os.path.join(self.repo_path, candidate)):
                return candidate
        return None

    def _component_object(self, root):
        """The options object of `export default {...}` or
        `export default defineComponent({...})`, if any."""
        for stmt in root.children:
            if stmt.type != "export_statement":
                continue
            value = stmt.child_by_field_name("value") or stmt.child_by_field_name("declaration")
            if value is None:
                value = next(
                    (c for c in stmt.named_children if c.type in ("object", "call_expression")),
                    None,
                )
            if value is not None and value.type == "call_expression":
                args = value.child_by_field_name("arguments")
                value = (
                    next((c for c in args.named_children if c.type == "object"), None)
                    if args
                    else None
                )
            if value is not None and value.type == "object":
                return value
        return None

    def _extract_options(self, root, lines):
        options = self._component_object(root)
        if options is None:
            return
        for prop in options.named_children:
            key, value = self._property(prop)
            if key in _MEMBER_GROUPS and value is not None and value.type == "object":
                for entry in value.named_children:
                    name, fn = self._property(entry)
                    if name and fn is not None:
                        self._add_member(name, entry, lines)
            elif key == "components" and value is not None and value.type == "object":
                for entry in value.named_children:
                    name, _ = self._property(entry)
                    if name and name not in self.imports:
                        self.imports[name] = name
            elif key in _LIFECYCLE_HOOKS and value is not None:
                self._add_member(key, prop, lines)

    def _property(self, prop):
        """`(key, function node | object | None)` for an object entry."""
        if prop.type == "method_definition":
            return self._text(prop.child_by_field_name("name")), prop
        if prop.type == "pair":
            key = self._text(prop.child_by_field_name("key")).strip("\"'")
            value = prop.child_by_field_name("value")
            if value is not None and (value.type in _FUNCTION_VALUES or value.type == "object"):
                return key, value
            return key, None
        if prop.type == "shorthand_property_identifier":
            return self._text(prop), None
        return None, None

    def _add_member(self, name: str, node, lines):
        if name in self.members:
            return
        logical = f"{self.component_name}.{name}"
        member_id = f"{self.relative_path}::{logical}"
        start, end = node.start_point[0], node.end_point[0] + 1
        self.nodes.append(
            Node(
                id=member_id,
                name=logical,
                component_type="method",
                file_path=str(self.file_path),
                relative_path=self.relative_path,
                source_code="\n".join(lines[start:end]),
                start_line=start + 1,
                end_line=end,
                node_type="method",
                class_name=self.component_name,
                display_name=f"method {logical}",
                component_id=member_id,
                language="vue",
            )
        )
        self.members[name] = member_id
        self._member_nodes[member_id] = node
        self._add_relationship(self.component_id, member_id, start + 1, resolved=True)

    def _extract_member_calls(self):
        for member_id, node in self._member_nodes.items():
            for call in self._descendants(node, "call_expression"):
                fn = call.child_by_field_name("function")
                line = call.start_point[0] + 1
                if fn is None:
                    continue
                if fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    prop = self._text(fn.child_by_field_name("property"))
                    if obj is not None and obj.type == "this" and prop in self.members:
                        self._add_relationship(member_id, self.members[prop], line, True)
                    elif obj is not None and obj.type == "identifier":
                        # `api.fetch()` through an imported module object.
                        self._add_name_edge(member_id, self._text(obj), line)
                elif fn.type == "identifier":
                    self._add_name_edge(member_id, self._text(fn), line)

    def _add_name_edge(self, caller_id: str, name: str, line: int):
        if name in self.members:
            self._add_relationship(caller_id, self.members[name], line, True)
        elif name in self.script_names:
            self._add_relationship(caller_id, self.script_names[name], line, True)
        elif name in self.imports:
            target = self.imports[name]
            self._add_relationship(caller_id, target, line, _is_sfc(target))

    # ------------------------------------------------------------------
    # Template
    # ------------------------------------------------------------------

    def _extract_template_edges(self, template):
        pascal_imports = {pascal_case(local): target for local, target in self.imports.items()}
        stack = [template]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            line = node.start_point[0] + 1
            if node.type in ("start_tag", "self_closing_tag") and node.parent is not template:
                tag = self._text(next((c for c in node.children if c.type == "tag_name"), None))
                if self._is_component_tag(tag):
                    name = pascal_case(tag)
                    target = pascal_imports.get(name, name)
                    self._add_relationship(self.component_id, target, line, _is_sfc(target))
            elif node.type == "directive_attribute":
                value = next((c for c in node.children if c.type == "quoted_attribute_value"), None)
                if value is not None:
                    self._expression_edges(self._text(value).strip("\"'"), line)
            elif node.type == "interpolation":
                self._expression_edges(self._text(node).strip("{} "), line)

    @staticmethod
    def _is_component_tag(tag: str) -> bool:
        if not tag or tag.lower() in _BUILTIN_TAGS:
            return False
        return "-" in tag or tag[0].isupper()

    def _expression_edges(self, expression: str, line: int):
        for name in _IDENT_RE.findall(expression):
            if name not in _JS_KEYWORDS:
                self._add_name_edge(self.component_id, name, line)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_component_node(self, lines):
        self.nodes.append(
            Node(
                id=self.component_id,
                name=self.component_name,
                component_type="class",
                file_path=str(self.file_path),
                relative_path=self.relative_path,
                source_code=self.content,
                start_line=1,
                end_line=max(len(lines), 1),
                node_type="component",
                display_name=f"Vue component {self.component_name}",
                component_id=self.component_id,
                language="vue",
            )
        )

    @staticmethod
    def _descendants(node, node_type: str):
        stack = list(node.children)
        while stack:
            current = stack.pop()
            if current.type == node_type:
                yield current
            stack.extend(current.children)

    def _add_relationship(self, caller: str, callee: str, line: int, resolved: bool):
        if not callee or caller == callee:
            return
        key = (caller, callee)
        if key in self.seen_relationships:
            return
        self.seen_relationships.add(key)
        self.call_relationships.append(
            CallRelationship(caller=caller, callee=callee, call_line=line, is_resolved=resolved)
        )


def analyze_vue_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterVueAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
