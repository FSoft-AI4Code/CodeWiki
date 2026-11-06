"""Lean 4 analyzer: a header scanner for structure, tree-sitter for references.

The tree-sitter Lean grammar (``lean`` in tree-sitter-language-pack) parses
most declarations, but it fails on many commands and notations (``#eval``,
``¬``, ``←``, ``do`` blocks, user notation), and it gives ``end`` as a flat
node, so the tree does not show namespace nesting. This analyzer therefore
uses two layers:

1. **Structure, from a header scanner.** Comments and strings are masked,
   and the Lean commands are read line by line: declarations (``def``,
   ``theorem``/``lemma``, ``abbrev``, ``instance``, ``structure``, ``class``,
   ``inductive``, ``axiom``, ``opaque``), the ``namespace``/``section``/
   ``mutual``/``end`` stack, ``open`` (also ``open X in``), ``import``,
   ``/-- -/`` docstrings and spans. The scanner does not depend on the
   parse tree, so an ``ERROR`` region does not hide a declaration.
2. **References, from tree-sitter.** Every ``identifier`` node in a
   declaration's lines is a reference candidate, also inside ``ERROR``
   nodes. Text that an ``ERROR`` node does not cover with child nodes is
   tokenized from the masked source. Local names (binders, ``let``,
   ``have``, ``fun``, ``intro``, match patterns) are removed, and so is a
   dotted name whose first part is local (``p.x``).

Each declaration is keyed by its fully qualified name (FQN). Structures and
classes also answer to their constructor and field names (``Point.mk``,
``Point.x``), and inductives to their constructors (``Tree.node``). The
analyzer emits each reference as written. ``CallGraphAnalyzer`` resolves it
to an exact FQN only, in Lean's order: the namespaces of the declaration
(innermost first), the root namespace, then the open namespaces. Only the
file itself and the files it imports (transitively) are visible, and a
``private`` name is visible only in its own file. A reference that matches
no declaration, or more than one, is dropped. A bare name is never guessed.

Known limitations:
- Generalized field notation (``v.norm`` for ``Vec.norm``) needs type
  information, so it does not resolve.
- References made through user ``notation``/``macro`` are lost.
- The scanner assumes that each command starts a line, which is the usual
  Lean style. A declaration ends at the next command at the same or a
  smaller indentation.
- An anonymous ``instance`` gets the synthetic name ``instance_L<line>``,
  not the name that Lean generates. ``example`` makes no component.
- Local names are removed by name for the whole declaration, so a global
  with the same simple name as a local of that declaration is not linked.
"""

import bisect
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node
from codewiki.src.be.dependency_analyzer.utils.file_selection import is_skipped_lean_file
from codewiki.src.be.dependency_analyzer.utils.grammars import get_pack_parser

logger = logging.getLogger(__name__)

# `component_type` per declaration keyword; `node_type` keeps the keyword.
COMPONENT_TYPES = {
    "structure": "struct",
    "class": "interface",
    "inductive": "class",
    "class inductive": "class",
}

_IDENT_PART = r"(?:«[^»\n]*»|[^\W\d][\w'!?₀-₉ₐ-ₜ]*)"
IDENT = rf"{_IDENT_PART}(?:\.(?:{_IDENT_PART}|\d+))*"
_IDENT_RE = re.compile(IDENT)

_MODIFIERS = r"(?:private|protected|noncomputable|partial|unsafe|nonrec|scoped|local|public|meta)"
_ATTRS = r"(?:@\[[^\]\n]*\]\s*)*"
_DECL_RE = re.compile(
    rf"^(?P<indent>\s*){_ATTRS}(?P<mods>(?:{_MODIFIERS}\s+)*)"
    r"(?P<kind>class\s+inductive|def|theorem|lemma|abbrev|instance|structure|class|inductive"
    r"|axiom|opaque|example)\b(?P<rest>.*)$"
)
_ATTR_ONLY_RE = re.compile(r"^\s*@\[[^\]\n]*\]\s*$")
_IMPORT_RE = re.compile(r"^\s*(?:(?:public|meta|private)\s+)*import\s+(?P<mods>.+)$")
_NAMESPACE_RE = re.compile(rf"^\s*namespace\s+(?P<name>{IDENT})")
_SECTION_RE = re.compile(rf"^\s*(?:noncomputable\s+)?section\b\s*(?P<name>{IDENT})?")
_MUTUAL_RE = re.compile(r"^\s*mutual\b")
_END_RE = re.compile(rf"^\s*end\b\s*(?P<name>{IDENT})?\s*$")
_OPEN_RE = re.compile(r"^\s*(?:(?:public|scoped|local)\s+)*(?:open|export)\s+(?P<body>.*)$")
# Commands that end the span of the declaration before them.
_COMMAND_RE = re.compile(
    r"^\s*(?:@\[|#|(?:"
    r"import|namespace|section|end|open|export|mutual|attribute|variable|universe|set_option"
    r"|notation|infix|infixl|infixr|prefix|postfix|macro|macro_rules|syntax|elab|elab_rules"
    r"|deriving|initialize|builtin_initialize|declare_syntax_cat|add_decl_doc|omit|include"
    r"|run_cmd"
    r")\b)"
)
_NAME_AFTER_KIND_RE = re.compile(rf"^\s*(?P<name>{IDENT})")
_INSTANCE_NAME_RE = re.compile(rf"^\s*(?:\(\s*priority\s*:=[^)]*\)\s*)?(?P<name>{IDENT})?\s*")
_EXTENDS_RE = re.compile(r"\bextends\s+(?P<bases>[^:=]*?)(?:\bwhere\b|:=|:|$)")
_STRUCT_CTOR_RE = re.compile(rf"^\s*(?P<name>{IDENT})\s*::\s*$")
_FIELD_RE = re.compile(rf"^\s*\(?\s*(?P<names>{IDENT}(?:\s+{IDENT})*)\s*(?::(?!=)|:=)")

# Local names introduced in a declaration's text. Tree rules cover the parsed
# parts; these also cover `ERROR` regions and tactic blocks.
_LOCAL_RES = [
    re.compile(r"\bfun\s+(?P<names>[^=]*?)=>"),
    re.compile(r"(?:∀|∃|∃!|Σ|Π|λ)\s*(?P<names>[^,]*?),"),
    re.compile(rf"\blet\s+(?:mut\s+|rec\s+)?(?P<names>{IDENT})"),
    re.compile(rf"\bhave\s+(?P<names>{IDENT})\s*:"),
    re.compile(rf"\bfor\s+(?P<names>{IDENT})\s+in\b"),
    re.compile(rf"[({{⦃\[]\s*(?P<names>{IDENT}(?:\s+{IDENT})*)\s*:(?!=)"),
    re.compile(rf"\(\s*(?P<names>{IDENT})\s*:="),
    re.compile(
        r"\b(?:intro|intros|rintro|rename_i|next|case|obtain|rcases|cases'|induction')\s+(?P<names>[^\n:=]*)"
    ),
    re.compile(rf"(?P<names>{IDENT})\s*←"),
]

_KEYWORDS = {
    "fun", "let", "have", "show", "from", "by", "do", "then", "else", "if", "match", "with",
    "at", "in", "for", "return", "where", "deriving", "mut", "rec", "Type", "Sort", "Prop",
    "true", "false", "True", "False", "this", "_", "sorry", "calc", "suffices", "using",
}  # fmt: skip

_LOCAL_BINDER_PARENTS = {"binders"}
_BINDER_TYPES = {
    "explicit_binder",
    "implicit_binder",
    "inst_binder",
    "strict_implicit_binder",
}
_INTRO_TACTICS = {"intro", "intros", "rintro", "obtain", "rcases", "rename_i", "next", "case"}


@lru_cache(maxsize=1)
def _lean_parser():
    return get_pack_parser("lean")


# ----------------------------------------------------------------------
# Masking
# ----------------------------------------------------------------------


@dataclass
class _Masked:
    text: str  # same lines and the same UTF-8 byte offsets as the source
    docstrings: list = field(default_factory=list)  # (end line index, text)


_RAW_STRING_RE = re.compile(r'r(#*)"')
_MASK_START_RE = re.compile(r"--|/-|r#*\"|\"|'")
_BLOCK_TOKEN_RE = re.compile(r"/-|-/")
_NOT_NEWLINE_RE = re.compile(rb"[^\n]")
_CHAR_RE = re.compile(r"'(?:\\[^']*|[^\\'\n])'")


def _blank(text: str) -> str:
    return _NOT_NEWLINE_RE.sub(b" ", text.encode("utf8")).decode("ascii")


def mask_source(source: str) -> _Masked:
    """Blank comments, strings and char literals with spaces of the same
    UTF-8 length, so lines and byte offsets do not move. Docstrings
    (``/-- -/``) are kept aside with the line on which they end."""
    out: list[str] = []
    docstrings = []
    i, n = 0, len(source)
    line, counted = 0, 0  # newlines before `counted`, so line numbers stay linear

    while i < n:
        m = _MASK_START_RE.search(source, i)
        if m is None:
            out.append(source[i:])
            break
        start, token = m.start(), m.group(0)
        out.append(source[i:start])
        if token == "--":
            j = source.find("\n", start)
            j = n if j < 0 else j
        elif token == "/-":
            depth, j = 1, start + 2
            while depth:
                b = _BLOCK_TOKEN_RE.search(source, j)
                if b is None:
                    j = n
                    break
                depth += 1 if b.group(0) == "/-" else -1
                j = b.end()
            if source.startswith("/--", start):
                line += source.count("\n", counted, j)
                counted = j
                docstrings.append((line, _clean_docstring(source[start:j])))
        elif token != '"' and _ident_before(source, start):
            # `add_comm'` or `r"` after a name: not a literal.
            out.append(source[start])
            i = start + 1
            continue
        elif token == "'":
            c = _CHAR_RE.match(source, start)
            if c is None:
                out.append(token)
                i = start + 1
                continue
            j = c.end()
        else:
            j = _string_end(source, start)
        out.append(_blank(source[start:j]))
        i = j
    return _Masked("".join(out), docstrings)


def _ident_before(source: str, i: int) -> bool:
    return i > 0 and (source[i - 1].isalnum() or source[i - 1] in "_'!?»")


def _string_end(source: str, i: int) -> int:
    n = len(source)
    if source[i] == "r":
        hashes = len(_RAW_STRING_RE.match(source, i).group(1))
        close = '"' + "#" * hashes
        j = source.find(close, i + 2 + hashes)
        return n if j < 0 else j + len(close)
    j = i + 1
    while j < n:
        if source[j] == "\\":
            j += 2
        elif source[j] == '"':
            return j + 1
        else:
            j += 1
    return n


def _clean_docstring(block: str) -> str:
    body = block[3:-2] if block.endswith("-/") else block[3:]
    return "\n".join(line.strip() for line in body.strip().splitlines()).strip()


# ----------------------------------------------------------------------
# Header scanner
# ----------------------------------------------------------------------


@dataclass
class LeanDecl:
    fqn: str
    kind: str
    start: int  # 0-based line of the first attribute or keyword line
    keyword_line: int
    end: int  # 0-based, inclusive
    indent: int
    private: bool
    namespaces: list[str]  # the declaration's namespace prefixes, innermost first
    opens: list[str]
    docstring: str = ""
    aliases: list[str] = field(default_factory=list)  # constructor and field FQNs
    bases: list[str] = field(default_factory=list)


@dataclass
class _Scope:
    kind: str  # "namespace", "section" or "mutual"
    name: str
    opens: list[str] = field(default_factory=list)


def _qualify(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _strip_guillemets(name: str) -> str:
    return name.replace("«", "").replace("»", "")


def _parse_open_names(body: str) -> list[str]:
    """`open A B (x y) hiding z renaming p → q` -> ["A", "B"]."""
    body = re.split(r"\b(?:hiding|renaming)\b", body)[0]
    body = re.sub(r"\([^)]*\)", " ", body)
    return [_strip_guillemets(m.group(0)) for m in _IDENT_RE.finditer(body)]


def scan_headers(masked: _Masked) -> tuple[list[LeanDecl], list[str]]:
    """Return the declarations and the imported module names of a file."""
    lines = masked.text.split("\n")
    docs = {end: text for end, text in masked.docstrings}
    decls: list[LeanDecl] = []
    imports: list[str] = []
    stack: list[_Scope] = []
    file_opens: list[str] = []
    pending_open_in: list[str] = []
    attr_start: int | None = None

    def namespace() -> str:
        return ".".join(s.name for s in stack if s.kind == "namespace")

    def active_opens() -> list[str]:
        return file_opens + [o for s in stack for o in s.opens]

    def close_previous(line_idx: int, indent: int) -> None:
        if decls and decls[-1].end < 0 and indent <= decls[-1].indent:
            end = line_idx - 1
            while end > decls[-1].keyword_line and not lines[end].strip():
                end -= 1
            decls[-1].end = end

    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if decls and decls[-1].end < 0 and indent > decls[-1].indent:
            continue  # a body line of the open declaration, even if it looks like a command
        decl_m = _DECL_RE.match(line)
        if decl_m is None and not _COMMAND_RE.match(line):
            continue  # a continuation line at the declaration's indentation
        close_previous(idx, indent)

        if _ATTR_ONLY_RE.match(line):
            if attr_start is None:
                attr_start = idx
            continue
        start = attr_start if attr_start is not None else idx
        attr_start = None

        if decl_m is not None:
            decl = _decl_from_match(decl_m, idx, start, namespace(), pending_open_in)
            pending_open_in = []
            if decl is None:
                continue
            decl.opens = active_opens() + decl.opens
            decl.docstring = _docstring_before(docs, lines, start)
            decls.append(decl)
            continue

        # `open X in` covers only the next command, whatever it is.
        pending_open_in = []
        if m := _IMPORT_RE.match(line):
            imports.extend(
                _strip_guillemets(x.group(0)) for x in _IDENT_RE.finditer(m.group("mods"))
            )
        elif m := _NAMESPACE_RE.match(line):
            stack.append(_Scope("namespace", _strip_guillemets(m.group("name"))))
        elif _MUTUAL_RE.match(line):
            stack.append(_Scope("mutual", ""))
        elif m := _END_RE.match(line):
            if stack:
                stack.pop()
        elif m := _SECTION_RE.match(line):
            stack.append(_Scope("section", m.group("name") or ""))
        elif m := _OPEN_RE.match(line):
            body = m.group("body")
            parts = re.split(r"\bin\b", body, maxsplit=1)
            names = _parse_open_names(parts[0])
            if len(parts) == 2:
                pending_open_in = names
                rest = parts[1]
                if rest.strip() and (inner := _DECL_RE.match(" " * indent + rest.strip())):
                    decl = _decl_from_match(inner, idx, start, namespace(), pending_open_in)
                    pending_open_in = []
                    if decl is not None:
                        decl.opens = active_opens() + decl.opens
                        decl.docstring = _docstring_before(docs, lines, start)
                        decls.append(decl)
            elif stack:
                stack[-1].opens.extend(names)
            else:
                file_opens.extend(names)

    if decls and decls[-1].end < 0:
        end = len(lines) - 1
        while end > decls[-1].keyword_line and not lines[end].strip():
            end -= 1
        decls[-1].end = end
    for decl in decls:
        if decl.kind in ("structure", "class", "inductive", "class inductive"):
            decl.aliases = _member_aliases(decl, lines)
    return decls, imports


def _decl_from_match(m, idx, start, ns, open_in) -> LeanDecl | None:
    kind = re.sub(r"\s+", " ", m.group("kind"))
    rest = m.group("rest")
    if kind == "example":
        name = None
    elif kind == "instance":
        nm = _INSTANCE_NAME_RE.match(rest)
        name = nm.group("name") if nm else None
        if name in _KEYWORDS:
            name = None
        if name is None:
            name = f"instance_L{idx + 1}"
    else:
        nm = _NAME_AFTER_KIND_RE.match(rest)
        name = nm.group("name") if nm else None
    if not name:
        # `example` and unnamed forms still end the previous declaration.
        return None
    name = _strip_guillemets(name)
    if name.startswith("_root_."):
        fqn = name[len("_root_.") :]
    else:
        fqn = _qualify(ns, name)
    parts = fqn.split(".")
    namespaces = [".".join(parts[:k]) for k in range(len(parts) - 1, 0, -1)]
    bases = []
    if kind in ("structure", "class") and (em := _EXTENDS_RE.search(rest)):
        bases = [_strip_guillemets(b.group(0)) for b in _IDENT_RE.finditer(em.group("bases"))]
    return LeanDecl(
        fqn=fqn,
        kind=kind,
        start=start,
        keyword_line=idx,
        end=-1,
        indent=len(m.group("indent")),
        private="private" in m.group("mods").split(),
        namespaces=namespaces,
        opens=list(open_in),
        bases=bases,
    )


def _docstring_before(docs: dict, lines: list[str], start: int) -> str:
    line = start - 1
    while line >= 0 and not lines[line].strip():
        if line in docs:
            return docs[line]
        line -= 1
    return docs.get(line, "") if line >= 0 else ""


def _member_aliases(decl: LeanDecl, lines: list[str]) -> list[str]:
    aliases: list[str] = []
    body = lines[decl.keyword_line + 1 : decl.end + 1]
    header = lines[decl.keyword_line]
    if decl.kind in ("inductive", "class inductive"):
        for line in [header.split(" where", 1)[-1], *body]:
            for chunk in line.split("|")[1:]:
                if m := _IDENT_RE.match(chunk.strip()):
                    aliases.append(_qualify(decl.fqn, _strip_guillemets(m.group(0))))
        return aliases
    ctor = "mk"
    if " where" in header or header.rstrip().endswith("where"):
        for line in body:
            if line.strip().startswith("deriving"):
                break
            if m := _STRUCT_CTOR_RE.match(line):
                ctor = _strip_guillemets(m.group("name"))
            elif m := _FIELD_RE.match(line):
                for name in m.group("names").split():
                    aliases.append(_qualify(decl.fqn, _strip_guillemets(name)))
    aliases.append(_qualify(decl.fqn, ctor))
    return aliases


# ----------------------------------------------------------------------
# References
# ----------------------------------------------------------------------


def _node_text(node) -> str:
    return node.text.decode("utf8", errors="replace")


def _collect_tree_names(root, masked_bytes: bytes):
    """Return (references, locals): references as (line, name); locals as
    (line, simple name)."""
    refs: list[tuple[int, str]] = []
    locals_: list[tuple[int, str]] = []
    line_starts = [0] + [m.end() for m in re.finditer(rb"\n", masked_bytes)]
    skip: set[int] = set()  # ids of identifier nodes that are not references

    def mark_local(node) -> None:
        for ident in _identifiers(node):
            skip.add(ident.id)
            text = _node_text(ident)
            if "." not in text:
                locals_.append((ident.start_point[0], text))

    stack = [root]
    while stack:
        node = stack.pop()
        t = node.type
        children = node.children
        if t in _LOCAL_BINDER_PARENTS:
            for child in children:
                if child.type == "identifier":
                    mark_local(child)
        elif t in _BINDER_TYPES:
            for child in children:
                if child.type == ":":
                    break
                if child.type == "identifier":
                    mark_local(child)
        elif t in ("let", "have"):
            for child in children[1:]:
                if child.type == "identifier":
                    mark_local(child)
                    break
                if child.type in (":=", ":", "←"):
                    break
        elif t == "match_alt":
            for child in children:
                if child.type == "=>":
                    break
                for ident in _identifiers(child):
                    if "." not in _node_text(ident):
                        mark_local(ident)
        elif t in ("struct_field", "field"):
            for child in children:
                if child.type in (":=", ":"):
                    break
                if child.type == "identifier":
                    skip.add(child.id)
        elif t == "app" and children and children[0].type == "identifier":
            if _node_text(children[0]) in _INTRO_TACTICS:
                for child in children[1:]:
                    mark_local(child)
        elif t == "ERROR":
            covered = 0
            for child in children:
                if child.start_byte > covered:
                    _tokens_in(
                        masked_bytes,
                        line_starts,
                        max(covered, node.start_byte),
                        child.start_byte,
                        refs,
                    )
                covered = max(covered, child.end_byte)
            if covered < node.end_byte:
                _tokens_in(
                    masked_bytes, line_starts, max(covered, node.start_byte), node.end_byte, refs
                )
        if t == "identifier" and node.id not in skip:
            refs.append((node.start_point[0], _node_text(node)))
        stack.extend(reversed(children))
    return refs, locals_


def _identifiers(node):
    if node.type == "identifier":
        yield node
        return
    for child in node.children:
        yield from _identifiers(child)


def _tokens_in(
    masked_bytes: bytes, line_starts: list[int], start: int, end: int, out: list
) -> None:
    if end <= start:
        return
    chunk = masked_bytes[start:end].decode("utf8", errors="replace")
    base_line = bisect.bisect_right(line_starts, start) - 1
    for m in _IDENT_RE.finditer(chunk):
        out.append((base_line + chunk.count("\n", 0, m.start()), m.group(0)))


def _text_locals(text: str) -> set[str]:
    names: set[str] = set()
    for regex in _LOCAL_RES:
        for m in regex.finditer(text):
            for ident in _IDENT_RE.finditer(m.group("names")):
                if "." not in ident.group(0):
                    names.add(ident.group(0))
    return names - _KEYWORDS


def normalize_reference(name: str) -> str | None:
    """Strip `@`, guillemets and numeric projections; drop `.foo` and keywords."""
    name = name.lstrip("@")
    if not name or name.startswith("."):
        return None
    parts = [p for p in _strip_guillemets(name).split(".") if p]
    while parts and parts[-1].isdigit():
        parts.pop()
    if not parts or parts[0] in _KEYWORDS:
        return None
    return ".".join(parts)


# ----------------------------------------------------------------------
# Analyzer
# ----------------------------------------------------------------------


class TreeSitterLeanAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = Path(file_path)
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        self.imports: list[str] = []
        # node id -> (namespace prefixes innermost first, open namespaces)
        self.scopes: dict[str, tuple[list[str], list[str]]] = {}
        # constructor/field FQN -> node id of the structure or inductive
        self.aliases: dict[str, str] = {}
        self.private_ids: set[str] = set()
        self.relative_path = Path(self._relative_path()).as_posix()
        if is_skipped_lean_file(self.relative_path):
            return
        try:
            self._analyze()
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Lean file {self.file_path}: {e}")

    def _relative_path(self) -> str:
        if self.repo_path:
            try:
                return os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                return str(self.file_path)
        return str(self.file_path)

    def _component_id(self, fqn: str) -> str:
        return f"{self.relative_path}::{fqn}"

    def _analyze(self) -> None:
        masked = mask_source(self.content)
        decls, self.imports = scan_headers(masked)
        source_lines = self.content.split("\n")
        by_fqn: dict[str, Node] = {}
        owners: list[tuple[LeanDecl, Node]] = []
        for decl in decls:
            if decl.fqn in by_fqn:
                continue
            node = self._add_node(decl, source_lines)
            by_fqn[decl.fqn] = node
            owners.append((decl, node))
            for alias in decl.aliases:
                self.aliases.setdefault(alias, node.id)
        if not owners:
            return

        tree = _lean_parser().parse(self.content.encode("utf8"))
        masked_bytes = masked.text.encode("utf8")
        refs, tree_locals = _collect_tree_names(tree.root_node, masked_bytes)
        line_owner: dict[int, tuple[LeanDecl, Node]] = {}
        for decl, node in owners:
            for line in range(decl.keyword_line, decl.end + 1):
                line_owner.setdefault(line, (decl, node))

        locals_by_decl: dict[str, set[str]] = {}
        masked_lines = masked.text.split("\n")
        for decl, node in owners:
            text = "\n".join(masked_lines[decl.keyword_line : decl.end + 1])
            locals_by_decl[node.id] = _text_locals(text)
        for line, name in tree_locals:
            if line in line_owner:
                locals_by_decl[line_owner[line][1].id].add(name)

        seen: set[tuple[str, str]] = set()
        for line, raw in refs:
            owner = line_owner.get(line)
            if owner is None:
                continue
            decl, node = owner
            name = normalize_reference(raw)
            if name is None or name.split(".")[0] in locals_by_decl[node.id]:
                continue
            if line == decl.keyword_line and name == decl.fqn.split(".")[-1]:
                continue  # the declaration's own name
            key = (node.id, name)
            if key in seen:
                continue
            seen.add(key)
            self.call_relationships.append(
                CallRelationship(caller=node.id, callee=name, call_line=line + 1)
            )

    def _add_node(self, decl: LeanDecl, source_lines: list[str]) -> Node:
        component_id = self._component_id(decl.fqn)
        component_type = COMPONENT_TYPES.get(decl.kind, "function")
        node_type = "theorem" if decl.kind == "lemma" else decl.kind
        node = Node(
            id=component_id,
            name=decl.fqn,
            component_type=component_type,
            file_path=str(self.file_path),
            relative_path=self.relative_path,
            source_code="\n".join(source_lines[decl.start : decl.end + 1]),
            start_line=decl.start + 1,
            end_line=decl.end + 1,
            has_docstring=bool(decl.docstring),
            docstring=decl.docstring,
            node_type=node_type,
            base_classes=decl.bases or None,
            display_name=f"{node_type} {decl.fqn}",
            component_id=component_id,
            language="lean",
            qualified_name=decl.fqn,
        )
        self.nodes.append(node)
        self.scopes[component_id] = (decl.namespaces, decl.opens)
        if decl.private:
            self.private_ids.add(component_id)
        return node


def analyze_lean_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterLeanAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
