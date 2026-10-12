"""What does changing, renaming or removing a symbol affect?

Deterministic and LLM-free. The dependency graph is function/class level, so
variable- and parameter-level questions need the source itself:

1. find every whole-word occurrence of the name in the live checkout (code
   and non-code files: YAML/JSON/TOML/templates/docs);
2. in Python files, label each occurrence from the AST (definition, write,
   read, attribute, import, re-export via ``__all__``, keyword argument at a
   call site, string literal) and drop other-scope hits (a local of the
   same name) and comment/docstring mentions;
3. map each site to its enclosing component and walk callers two hops out;
4. flag tests, public API exposure and dynamic access the analysis cannot
   see (``getattr``/string keys/environment names).
"""

from __future__ import annotations

import ast
import os
import re
import textwrap
from dataclasses import dataclass, field

from codewiki.src.be.qa.index import QAIndex

CALLER_HOPS = 2
_MAX_FILES = 3000
_MAX_BYTES = 1_000_000
_CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env", ".properties"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox"}
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", ";")
_DYNAMIC_CALLS = {"getattr", "setattr", "hasattr", "delattr"}

USAGE_KINDS = ("write", "read", "attribute", "keyword", "import", "reexport", "reference", "config")


@dataclass
class Site:
    file: str
    line: int
    kind: str
    text: str
    component: str | None = None

    def render(self) -> str:
        where = f" in {self.component}" if self.component else ""
        return f"{self.file}:{self.line} · {self.kind}{where} · {self.text}"


@dataclass
class ImpactReport:
    symbol: str
    change: str
    definitions: list[Site] = field(default_factory=list)
    usages: list[Site] = field(default_factory=list)
    textual: list[Site] = field(default_factory=list)
    other_scope: list[Site] = field(default_factory=list)
    dynamic: list[Site] = field(default_factory=list)
    affected: list[str] = field(default_factory=list)  # components containing a site
    callers: list[str] = field(default_factory=list)  # their callers, not already affected
    tests: list[str] = field(default_factory=list)
    public_api: list[Site] = field(default_factory=list)
    doc_pages: list[str] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def all_ids(self) -> list[str]:
        return list(dict.fromkeys(self.affected + self.callers))

    def render(self, max_sites: int = 60) -> str:
        out = [f"Impact of {self.change} `{self.symbol}`"]

        def section(title: str, sites: list[Site], limit: int = max_sites) -> None:
            if not sites:
                return
            out.append(f"\n{title} ({len(sites)}):")
            out.extend(f"- {s.render()}" for s in sites[:limit])
            if len(sites) > limit:
                out.append(f"- … {len(sites) - limit} more")

        section("Definitions", self.definitions)
        section("Usages", self.usages)
        section("Public API exposure", self.public_api)
        section("Dynamic access (not visible to static analysis)", self.dynamic)
        if self.affected:
            out.append(
                f"\nComponents that use it ({len(self.affected)}): {', '.join(self.affected)}"
            )
        if self.callers:
            out.append(
                f"Callers of those, up to {CALLER_HOPS} hops ({len(self.callers)}): "
                + ", ".join(self.callers[:40])
                + (" …" if len(self.callers) > 40 else "")
            )
        if self.tests:
            out.append(f"Tests that touch it: {', '.join(self.tests)}")
        if self.doc_pages:
            out.append(f"Doc pages that mention it: {', '.join(self.doc_pages)}")
        section("Same name, different scope (excluded)", self.other_scope, 15)
        section("Mentions in comments/strings/docs (excluded)", self.textual, 15)
        if self.stale_files:
            out.append(
                "Note: these files changed since the docs were generated (references come from "
                f"the current files): {', '.join(self.stale_files)}"
            )
        if self.truncated:
            out.append("Note: the scan stopped at its file limit; there may be more references.")
        if len(out) == 1:
            out.append("No references found in the repository.")
        return "\n".join(out)


def is_test_path(rel: str) -> bool:
    parts = rel.lower().split("/")
    name = parts[-1]
    return (
        any(p in ("test", "tests", "__tests__", "spec", "specs") for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "_test.go"))
    )


class _PyScanner(ast.NodeVisitor):
    """Labels the occurrences of one name in one Python module."""

    def __init__(self, name: str, target_params: set[str]) -> None:
        self.name = name
        self.target_params = target_params  # functions whose *parameter* is the target
        self.sites: dict[int, str] = {}
        self.other_scope: set[int] = set()
        self.dynamic: set[int] = set()
        self.reexport: set[int] = set()
        self._scopes: list[tuple[str, set[str]]] = []  # (function name, local names)

    def _mark(self, line: int, kind: str) -> None:
        # Keep the most specific label per line (a definition beats a read).
        order = ["definition", "write", "keyword", "import", "attribute", "read"]
        cur = self.sites.get(line)
        if cur is None or order.index(kind) < order.index(cur):
            self.sites[line] = kind

    def _locals_of(self, node) -> set[str]:
        names = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
        if node.args.vararg:
            names.add(node.args.vararg.arg)
        if node.args.kwarg:
            names.add(node.args.kwarg.arg)
        declared_global: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Global, ast.Nonlocal)):
                declared_global.update(sub.names)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                names.add(sub.id)
        return names - declared_global

    def _function(self, node) -> None:
        if node.name == self.name:
            self._mark(node.lineno, "definition")
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if arg.arg == self.name:
                kind = "definition" if node.name in self.target_params else None
                if kind:
                    self._mark(arg.lineno, kind)
                else:
                    self.other_scope.add(arg.lineno)
        self._scopes.append((node.name, self._locals_of(node)))
        self.generic_visit(node)
        self._scopes.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == self.name:
            self._mark(node.lineno, "definition")
        self.generic_visit(node)

    def _in_target_scope(self) -> bool:
        """Is a bare Name here the target, rather than a same-named local?"""
        for func, local_names in reversed(self._scopes):
            if self.name in local_names:
                return func in self.target_params
        return not self.target_params  # module-level name (unless the target is a parameter)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == self.name:
            if not self._in_target_scope():
                self.other_scope.add(node.lineno)
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                self._mark(node.lineno, "write" if self._scopes else "definition")
            else:
                self._mark(node.lineno, "read")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.name:
            self._mark(node.lineno, "write" if isinstance(node.ctx, ast.Store) else "attribute")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg == self.name:
            self._mark(node.lineno if hasattr(node, "lineno") else node.value.lineno, "keyword")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if self.name in (alias.name, alias.asname):
                self._mark(node.lineno, "import")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self.name in (alias.name.split(".")[-1], alias.asname):
                self._mark(node.lineno, "import")

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            for elt in getattr(node.value, "elts", []):
                if isinstance(elt, ast.Constant) and elt.value == self.name:
                    self.reexport.add(elt.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if fname in _DYNAMIC_CALLS:
            for arg in node.args[1:2]:
                if isinstance(arg, ast.Constant) and arg.value == self.name:
                    self.dynamic.add(node.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value == self.name:
                self.dynamic.add(key.lineno)  # {"name": ...} passed on as **kwargs
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = node.slice
        if isinstance(key, ast.Constant) and key.value == self.name:
            self.dynamic.add(node.lineno)  # obj["name"], os.environ["NAME"]
        self.generic_visit(node)


def _repo_files(index: QAIndex):
    repo = index.repo_path
    docs = os.path.abspath(index.docs_dir)
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in _SKIP_DIRS
            and not d.startswith(".")
            and os.path.abspath(os.path.join(root, d)) != docs
        )
        for name in sorted(files):
            yield os.path.relpath(os.path.join(root, name), repo).replace(os.sep, "/")


def _parameters(source: str) -> set[str]:
    """Parameter names of the function defined at the top of ``source``."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            names = {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
            names |= {x.arg for x in (a.vararg, a.kwarg) if x}
            return names
    return set()


def _target_params(index: QAIndex, name: str, qualifier: str | None, file: str | None) -> set[str]:
    """Functions whose parameter is the target, when the symbol is a parameter.

    ``TimestampSigner.unsign.max_age`` or ``max_age`` with ``file`` naming the
    module: the target is the parameter if no module/class-level definition
    of the name exists there but a function there takes it as an argument.
    """
    out: set[str] = set()
    for unit in index.code.values():
        if file and unit.file != file:
            continue
        func = unit.id.split("::", 1)[-1]
        if qualifier and not func.endswith(qualifier):
            continue
        if unit.kind in ("function", "method") and name in _parameters(unit.source):
            out.add(func.split(".")[-1])
    if not out:
        return set()
    # A module- or class-level definition of the name wins over a parameter.
    for unit in index.code.values():
        if (not file or unit.file == file) and unit.id.split("::", 1)[-1].split(".")[-1] == name:
            return set()
    return out


def find_references(
    index: QAIndex, symbol: str, file: str | None = None, change: str = "modify"
) -> ImpactReport:
    symbol = symbol.strip().strip("`")
    parts = re.split(r"::|\.", symbol)
    name = parts[-1]
    qualifier = ".".join(parts[:-1]) or None
    if qualifier and "/" in qualifier:
        qualifier = None
    report = ImpactReport(symbol=symbol, change=change)
    if not name or not index.repo_path:
        return report
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    target_params = _target_params(index, name, qualifier, file)
    touched = index.freshness.touched
    usage_components: dict[str, None] = {}
    definition_components: dict[str, None] = {}
    tests: dict[str, None] = {}

    for count, rel in enumerate(_repo_files(index)):
        if count >= _MAX_FILES:
            report.truncated = True
            break
        path = os.path.join(index.repo_path, rel)
        try:
            if os.path.getsize(path) > _MAX_BYTES:
                continue
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        if not word.search(text):
            continue
        lines = text.splitlines()
        ext = os.path.splitext(rel)[1].lower()
        labels: dict[int, str] = {}
        excluded: set[int] = set()
        dynamic: set[int] = set()
        reexport: set[int] = set()
        if ext == ".py":
            try:
                scanner = _PyScanner(name, target_params)
                scanner.visit(ast.parse(text))
                labels, excluded = scanner.sites, scanner.other_scope
                dynamic, reexport = scanner.dynamic, scanner.reexport
            except SyntaxError:
                labels = {}
                ext = ".pytext"
        for no, line in enumerate(lines, 1):
            if not word.search(line):
                continue
            snippet = line.strip()[:140]
            unit = index.component_at(rel, no)
            cid = unit.id if unit else None
            if ext == ".py":
                kind = labels.get(no)
                if no in reexport:
                    report.public_api.append(Site(rel, no, "reexport", snippet, cid))
                    continue
                if no in dynamic:
                    report.dynamic.append(Site(rel, no, "dynamic", snippet, cid))
                    continue
                if kind is None:
                    target = report.other_scope if no in excluded else report.textual
                    target.append(
                        Site(rel, no, "other scope" if no in excluded else "text", snippet, cid)
                    )
                    continue
            elif ext in _CONFIG_EXT:
                kind = "config"
            elif snippet.startswith(_COMMENT_PREFIXES) or ext in (".md", ".rst", ".txt"):
                report.textual.append(Site(rel, no, "text", snippet, cid))
                continue
            else:
                kind = "reference"
            site = Site(rel, no, kind, snippet, cid)
            if kind == "definition":
                report.definitions.append(site)
                if cid:
                    definition_components[cid] = None
            else:
                report.usages.append(site)
                if cid:
                    usage_components[cid] = None
            if kind == "import" and rel.endswith("__init__.py"):
                report.public_api.append(site)
            if is_test_path(rel):
                tests[rel] = None

    report.affected = list(usage_components)
    seen = set(report.affected) | set(definition_components)
    frontier = list(seen)
    callers: list[str] = []
    for _ in range(CALLER_HOPS):
        nxt = []
        for cid in frontier:
            for caller in sorted(index.callers.get(cid, ())):
                if caller not in seen:
                    seen.add(caller)
                    callers.append(caller)
                    nxt.append(caller)
        frontier = nxt
    report.callers = callers
    report.tests = list(tests)
    report.doc_pages = sorted(
        # raw markdown: the plain search text drops "_" as emphasis (MAX_AGE -> MAXAGE)
        {sec.file for sec in index.sections.values() if word.search(sec.raw)}
    )
    report.stale_files = sorted(
        {s.file for s in report.definitions + report.usages if s.file in touched}
    )
    return report
