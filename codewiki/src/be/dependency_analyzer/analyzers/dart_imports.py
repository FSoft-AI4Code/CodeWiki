"""Dart import/export/part directives and repository-local URI resolution.

The Dart analyzer records each file's directives; once every file is
parsed, the call-graph analyzer resolves them to repository files and
computes two scopes per file:

- ``visible``: the caller's library (the file, the library it is a
  ``part of``, and that library's other parts) plus every library it
  imports, followed transitively through ``export`` directives. Name
  resolution prefers candidates inside this set.
- ``library_members``: the library alone. Names starting with ``_`` are
  library-private in Dart, so they may only resolve inside it.

``package:<name>/<path>`` maps to ``<dir of pubspec.yaml named name>/lib/<path>``;
``dart:`` URIs and packages not defined in the repository map to nothing.
"""

import os
import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

_PUBSPEC_NAME_RE = re.compile(r"^name:\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


@dataclass(frozen=True)
class DartDirective:
    kind: str  # "import" | "export" | "part" | "part_of"
    uri: str | None
    prefix: str | None = None
    line: int = 0


def _string_value(node) -> str | None:
    """The contents of the first string literal under ``node``, quotes stripped."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "string_literal":
            text = current.text.decode("utf8", errors="replace").lstrip("r")
            return text.strip("'\"")
        stack.extend(reversed(current.named_children))
    return None


def _first_child(node, node_type: str):
    return next((c for c in node.named_children if c.type == node_type), None)


def parse_directives(root) -> list[DartDirective]:
    directives: list[DartDirective] = []
    for child in root.named_children:
        line = child.start_point[0] + 1
        if child.type == "import_or_export":
            for sub in child.named_children:
                if sub.type == "library_import":
                    spec = _first_child(sub, "import_specification") or sub
                    uri_node = _first_child(spec, "configurable_uri") or _first_child(spec, "uri")
                    prefix = _first_child(spec, "identifier")
                    directives.append(
                        DartDirective(
                            "import",
                            _string_value(uri_node) if uri_node is not None else None,
                            prefix.text.decode() if prefix is not None else None,
                            line,
                        )
                    )
                elif sub.type == "library_export":
                    uri_node = _first_child(sub, "configurable_uri") or _first_child(sub, "uri")
                    directives.append(
                        DartDirective(
                            "export",
                            _string_value(uri_node) if uri_node is not None else None,
                            None,
                            line,
                        )
                    )
        elif child.type == "part_directive":
            uri_node = _first_child(child, "uri")
            directives.append(
                DartDirective("part", _string_value(uri_node) if uri_node else None, None, line)
            )
        elif child.type == "part_of_directive":
            uri_node = _first_child(child, "uri")
            directives.append(
                DartDirective("part_of", _string_value(uri_node) if uri_node else None, None, line)
            )
    return directives


def _read_package_name(pubspec_path: str) -> str | None:
    try:
        with open(pubspec_path, encoding="utf-8", errors="replace") as handle:
            match = _PUBSPEC_NAME_RE.search(handle.read())
    except OSError:
        return None
    return match.group(1) if match else None


class DartPackageResolver:
    def __init__(self, packages: dict[str, str]):
        self.packages = packages

    @classmethod
    def from_files(cls, repo_dir: str, dart_relpaths: Iterable[str]) -> "DartPackageResolver":
        """Find the pubspec.yaml of every directory that holds (or is an ancestor
        of) an analyzed Dart file. Walking ancestors of analyzed files, instead
        of the whole tree, never enters ignored directories."""
        packages: dict[str, str] = {}
        seen: set[str] = set()
        for relpath in dart_relpaths:
            directory = posixpath.dirname(relpath)
            while directory not in seen:
                seen.add(directory)
                pubspec = os.path.join(repo_dir, directory, "pubspec.yaml")
                if os.path.isfile(pubspec):
                    name = _read_package_name(pubspec)
                    if name and name not in packages:
                        packages[name] = directory
                if not directory:
                    break
                directory = posixpath.dirname(directory)
        return cls(packages)

    def resolve(self, uri: str | None, from_relpath: str) -> str | None:
        if not uri or uri.startswith("dart:"):
            return None
        if uri.startswith("package:"):
            name, _, rest = uri[len("package:") :].partition("/")
            root = self.packages.get(name)
            if root is None or not rest:
                return None
            target = posixpath.normpath(posixpath.join(root, "lib", rest))
        elif "://" in uri or uri.startswith("/"):
            return None
        else:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(from_relpath), uri))
        if target.startswith("../") or target == "..":
            return None
        return target


@dataclass
class DartScopes:
    visible: dict[str, set[str]]
    library_members: dict[str, set[str]]


def build_scopes(
    directives_by_file: dict[str, list[DartDirective]],
    resolver: DartPackageResolver,
    known_files: set[str],
) -> DartScopes:
    library_of: dict[str, str] = {}
    parts_of: dict[str, set[str]] = defaultdict(set)

    # The `part` side is authoritative; `part of '<uri>'` is the fallback.
    for path, directives in directives_by_file.items():
        for directive in directives:
            if directive.kind == "part":
                target = resolver.resolve(directive.uri, path)
                if target in known_files:
                    library_of[target] = path
                    parts_of[path].add(target)
    for path, directives in directives_by_file.items():
        if path in library_of:
            continue
        for directive in directives:
            if directive.kind == "part_of" and directive.uri:
                library = resolver.resolve(directive.uri, path)
                if library in known_files:
                    library_of[path] = library
                    parts_of[library].add(path)

    def members(path: str) -> set[str]:
        library = library_of.get(path, path)
        return {library} | parts_of.get(library, set())

    def targets(path: str, kind: str) -> set[str]:
        found: set[str] = set()
        for member in members(path):
            for directive in directives_by_file.get(member, []):
                if directive.kind == kind:
                    target = resolver.resolve(directive.uri, member)
                    if target in known_files:
                        found.add(target)
        return found

    def exported_closure(start: str) -> set[str]:
        result: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            library_files = members(current)
            if library_files <= result:
                continue
            result |= library_files
            stack.extend(targets(current, "export"))
        return result

    visible: dict[str, set[str]] = {}
    library_members: dict[str, set[str]] = {}
    for path in directives_by_file:
        own = members(path)
        scope = set(own)
        for imported in targets(path, "import"):
            scope |= exported_closure(imported)
        visible[path] = scope
        library_members[path] = own
    return DartScopes(visible=visible, library_members=library_members)


def is_private_dart_name(name: str) -> bool:
    """Library-private callee (`_Foo`, `Foo._bar`). Already-resolved
    component ids (containing `::`) are never treated as names."""
    if not name or "::" in name:
        return False
    return any(segment.startswith("_") for segment in name.split("."))
