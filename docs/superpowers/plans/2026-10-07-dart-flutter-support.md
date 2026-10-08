# Dart / Flutter Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Project rules for this plan:** Opus orchestrates and reviews; Sonnet builder agents implement one task each from this file; Sonnet tester agents run tests and the analysis harness. **Agents never commit.** "Checkpoint" steps mean: the orchestrator (main session) reviews `git diff` and commits that task (user-approved). This plan file itself is never staged (it lives in the repo's generated-docs folder).

**Goal:** CodeWiki analyzes `.dart` files: classes, mixins, extensions, enums, top-level functions, methods and imports (incl. `package:` and `part`) become graph components and edges, with Flutter-aware widget-composition and Riverpod edges and Flutter-aware documentation prompt hints.

**Architecture:** A new tree-sitter analyzer (`analyzers/dart.py`) mirrors the Rust/Scala analyzers: pass 1 extracts declarations as `Node`s, pass 2 emits `CallRelationship`s. Import/part directives are parsed by `analyzers/dart_imports.py`, which also resolves `package:`/relative URIs to repo files and computes, per file, the set of files whose declarations it can see; `CallGraphAnalyzer` uses that set to disambiguate cross-file name resolution for Dart callers. Flutter knowledge (widget/state/notifier classification, `build()` composition lifting, Riverpod `ref.watch`, providers, GoRouter) is isolated in `analyzers/dart_flutter.py`. A Dart/Flutter note is appended to the per-module user prompt when the module contains Dart files.

**Tech Stack:** Python ≥3.12, `tree-sitter==0.23.2` runtime, Dart grammar from `tree-sitter-language-pack==0.8.0` (`get_parser("dart")`, ABI 14), pytest, ruff.

**Spec:** the user's brief (in the conversation that produced this plan). Its requirements are copied into Global Constraints below.

## Global Constraints

- No new runtime dependency. Use `from tree_sitter_language_pack import get_parser` → `get_parser("dart")`. Do **not** add `tree-sitter-dart` (0.1.0 ships ABI 15; the pinned `tree-sitter==0.23.2` runtime rejects it with `ValueError: Incompatible Language version 15. Must be between 13 and 14`).
- Follow the Rust PR (#127) / Scala PR (#108) shape: one analyzer file per language, function `analyze_<lang>_file(file_path, content, repo_path=None) -> tuple[list[Node], list[CallRelationship]]`, `language="dart"` on every node, component ids `"<posix relpath>::<logical name>"`, methods keyed `Owner.method`.
- `component_type` values limited to what leaf selection understands: `"class"`, `"interface"`, `"method"`, `"function"`. The Dart/Flutter construct goes in `node_type` (`class`, `abstract class`, `sealed class`, `mixin class`, `interface class`, `interface`, `mixin`, `extension`, `enum`, `method`, `getter`, `function`, `widget`, `state`, `notifier`, `bloc`, `cubit`, `provider`, `router`).
- Tests that need the grammar start with `pytest.importorskip("tree_sitter_language_pack")`.
- Exactly one paid LLM run is approved (Task 9 Step 5); no others. Validation uses the analysis-only harness (Task 8), which never calls an LLM and writes only to the session scratchpad — never into the target repos.
- Primary test target: `~/Documents/Github/avto-consumption/app` (95 `lib/` Dart files, flutter_riverpod + go_router, no codegen). Final check: `~/Documents/Github/NorthStar/app` (688 `lib/` files, riverpod_generator codegen: 180 `@riverpod`, 34 `part` files).
- Code style: match surrounding code (ruff config in `pyproject.toml`; run `ruff check` and `ruff format --check` on touched files). Comment density like `analyzers/rust.py`.
- Env (Task 0) mirrors CI: `.venv` + `pip install -r requirements.txt` + `pip install pytest pytest-asyncio pytest-cov ruff` + `pip install -e . --no-deps`. All commands below use `.venv/bin/python -m pytest` / `.venv/bin/ruff`.

## Review Focus

1. **Duplicate private names across files** (`_Body`, `_HeaderState` in many screens) — an edge to `_Body` must bind to the caller's own library (file + its parts), never to another file's `_Body`, and must be dropped rather than mis-bound when not found. Pinned in Task 4 (`test_private_names_bind_within_library_only`).
2. **Files with parse errors** (`library;`, null-aware elements `'pin': ?pin`, `extension type`) — declarations outside the ERROR span are still extracted; the file never aborts the sweep. Pinned in Task 2 (`test_parse_errors_keep_surrounding_declarations`).
3. **Same public name in two files** (two `SettingsPage`s in different features) — the one the caller imports wins. Pinned in Task 4 (`test_imported_definition_wins_over_global_duplicate`).
4. **Generated files and codegen providers** — `*.g.dart` is ignored by default; `part 'x.g.dart'` pointing at an ignored file is harmless; `ref.watch(counterProvider)` resolves to the `@riverpod class Counter` (Riverpod 2 and 3 naming). Pinned in Task 1 (`test_part_to_missing_file_is_ignored`), Task 2 (`test_generated_dart_files_are_ignored`), Task 5 (`test_riverpod_codegen_alias_resolves`).
5. **`package:` URIs** — own package maps to `<pubspec dir>/lib/...`, external packages and `dart:` map to nothing, `../` escaping the repo maps to nothing, nested packages (monorepo) each map to their own `lib/`. Pinned in Task 1 (`test_package_resolver_*`).

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `codewiki/src/be/dependency_analyzer/analyzers/dart_imports.py` | Create | `DartDirective`, `parse_directives(root)`, `DartPackageResolver`, `DartScopes`, `build_scopes(...)`, `is_private_dart_name(name)` |
| `codewiki/src/be/dependency_analyzer/analyzers/dart.py` | Create | `TreeSitterDartAnalyzer`, `analyze_dart_file(...)`, noise sets |
| `codewiki/src/be/dependency_analyzer/analyzers/dart_flutter.py` | Create | Flutter/Riverpod/GoRouter tables and pure classifiers |
| `codewiki/src/be/dependency_analyzer/analysis/call_graph_analyzer.py` | Modify | dispatch, `_analyze_dart_file`, Dart scope building, Dart-aware resolution, `lang-dart` viz class |
| `codewiki/src/be/dependency_analyzer/utils/patterns.py` | Modify | include/extension/entry-point/definition patterns, ignore generated Dart files |
| `codewiki/src/be/dependency_analyzer/ast_parser.py` | Modify | `.dart` in extension list |
| `codewiki/src/be/dependency_analyzer/analysis/analysis_service.py` | Modify | `"dart"` in both supported-language collections |
| `codewiki/cli/utils/repo_validator.py`, `codewiki/cli/utils/validation.py`, `codewiki/mcp/tools/analysis.py`, `codewiki/cli/main.py` | Modify | extension lists, language names, help text |
| `codewiki/src/be/prompt_template.py` | Modify | `.dart` fence language, `DART_FLUTTER_NOTE` |
| `tests/test_dart_imports.py`, `tests/test_dart_analyzer.py`, `tests/test_dart_flutter.py` | Create | unit + end-to-end tests |
| `tests/test_doc_layout.py` | Modify | prompt-note tests |
| `README.md`, `CHANGELOG.md`, `guides/development.md` | Modify | docs |
| `$SCRATCH/analyze_only.py` | Create (scratchpad, not repo) | no-LLM analysis harness + metrics |

`$SCRATCH` = `/private/tmp/claude-501/-Users-maj-Documents-Github-CodeWiki/5439ea04-92d9-497d-bffd-96465493ee28/scratchpad`.

## Grammar facts (verified against tree-sitter-language-pack 0.8.0)

Builders: trust these; when unsure of a shape, dump it with the probe in Task 0 Step 4.

- Directives: `import_or_export > library_import > import_specification > (configurable_uri > uri > string_literal, identifier /*as-prefix*/, combinator*)`; `import_or_export > library_export > configurable_uri`; `part_directive > uri`; `part_of_directive > (part_of_builtin, uri)` (uri absent for `part of some.lib;`).
- `class_definition`: modifiers are child tokens (`abstract`, `sealed`, `interface`, `base`, `mixin`, `final` — `final` is anonymous); fields `name`, `type_parameters`, `superclass` (`type_identifier`, `type_arguments`, `mixins > type_identifier*`), `interfaces` (`type_identifier*`), `body` (`class_body`). Class annotations (`@riverpod`) are `annotation` **children** of `class_definition`; doc comments are the preceding `documentation_comment` sibling.
- `mixin_declaration`: `identifier` (name, no field), `type_identifier*` (the `on` types), `class_body`.
- `extension_declaration`: fields `name` (optional), `class` (the on-type), `body` (`extension_body`).
- `enum_declaration`: fields `name`, `body` (`enum_body` with `enum_constant*` and members).
- Members inside `class_body`/`extension_body`/`enum_body` are flat siblings: `annotation`, `documentation_comment`, then either `method_signature` followed by a sibling `function_body`, or `declaration` (abstract method, field, or body-less constructor). `method_signature`/`declaration` wrap one of `function_signature` (field `name`), `getter_signature` (field `name`), `setter_signature`, `operator_signature`, `constructor_signature`, `factory_constructor_signature`, `constant_constructor_signature`, `redirecting_factory_constructor_signature`.
- Top level: `function_signature`/`getter_signature` directly under `program`, followed by a sibling `function_body`; a top-level `@riverpod` is the preceding sibling `annotation`. Top-level `final x = …` is `final_builtin` + `static_final_declaration_list > static_final_declaration(identifier, value…)`.
- **No call node.** A call/member chain is a primary (`identifier`, `this`, `super`) followed by sibling `selector` nodes inside the same parent. `selector` contains either `unconditional_assignable_selector`/`conditional_assignable_selector` (`> identifier` = member name) or `argument_part > arguments > (argument | named_argument)*`. `ref.watch(carProvider)` = `identifier(ref) selector(.watch) selector((carProvider))`.
- Cascades: `identifier(repo) cascade_section(cascade_selector > identifier, argument_part)*`.
- `const Text('x')` = `const_object_expression(const_builtin, type_identifier, arguments)`; `new Foo<int>()` = `new_expression(type_identifier, type_arguments, arguments)`.
- Generic invocation `Bar<int>(1)` / `FutureProvider<Car>((ref) => …)` misparses as `relational_expression(relational_expression(identifier Bar, <, identifier int), >, parenthesized_expression)`.
- Locals: `local_variable_declaration > initialized_variable_definition(type_identifier?, identifier[name], value…)`; params: `formal_parameter(type_identifier?, identifier[name])`, `this.x` = `formal_parameter > constructor_param(this, identifier)`.
- Parse errors: `extension type X(int v) {}` → `ERROR`; `library;` and null-aware elements produce `ERROR`/missing nodes. Surrounding declarations still parse.

---

### Task 0: Environment and branch (orchestrator, needs user permission)

**Files:** none in repo (creates `.venv/`, already gitignored).

- [ ] **Step 1: Branch**

```bash
cd ~/Documents/Github/CodeWiki && git switch -c feat/dart-flutter-support
```

- [ ] **Step 2: CI-equivalent venv**

```bash
cd ~/Documents/Github/CodeWiki
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest pytest-asyncio pytest-cov ruff
.venv/bin/pip install -e . --no-deps
```

- [ ] **Step 3: Baseline suite**

Run: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" tests/ -q`
Expected: all pass (record the count; later tasks must keep them passing).

- [ ] **Step 4: Save the AST probe for builders** at `$SCRATCH/ast/dump.py`:

```python
"""Print the named-node tree of a Dart file: python dump.py file.dart"""
import sys

from tree_sitter_language_pack import get_parser


def walk(node, depth=0):
    if node.is_named:
        field = ""
        if node.parent is not None:
            for i, child in enumerate(node.parent.children):
                if child.id == node.id:
                    field = node.parent.field_name_for_child(i) or ""
        text = node.text.decode()[:50].replace("\n", " ") if len(node.text) < 50 else ""
        print("  " * depth + node.type + (f" [{field}]" if field else "") + (f"  «{text}»" if text else ""))
    for child in node.children:
        walk(child, depth + 1)


tree = get_parser("dart").parse(open(sys.argv[1], "rb").read())
walk(tree.root_node)
print("HAS_ERROR", tree.root_node.has_error)
```

Run: `.venv/bin/python $SCRATCH/ast/dump.py $SCRATCH/ast/sample.dart | head` → prints `program`.

---

### Task 1: Directive parsing, package resolution, visibility scopes (`dart_imports.py`)

**Files:**
- Create: `codewiki/src/be/dependency_analyzer/analyzers/dart_imports.py`
- Test: `tests/test_dart_imports.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `@dataclass(frozen=True) class DartDirective: kind: str  # "import"|"export"|"part"|"part_of"; uri: str | None; prefix: str | None = None; line: int = 0`
  - `parse_directives(root) -> list[DartDirective]` (`root` = tree-sitter `program` node)
  - `class DartPackageResolver: __init__(self, packages: dict[str, str]); packages: dict[str, str]  # name -> posix relpath of package dir ("" = repo root); @classmethod from_files(cls, repo_dir: str, dart_relpaths: Iterable[str]) -> DartPackageResolver; resolve(self, uri: str | None, from_relpath: str) -> str | None`
  - `@dataclass class DartScopes: visible: dict[str, set[str]]; library_members: dict[str, set[str]]`
  - `build_scopes(directives_by_file: dict[str, list[DartDirective]], resolver: DartPackageResolver, known_files: set[str]) -> DartScopes`
  - `is_private_dart_name(name: str) -> bool`

- [ ] **Step 1: Write the failing tests** — `tests/test_dart_imports.py`:

```python
"""Tests for Dart directive parsing, URI resolution and visibility scopes."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from tree_sitter_language_pack import get_parser

from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
    DartDirective,
    DartPackageResolver,
    build_scopes,
    is_private_dart_name,
    parse_directives,
)


def _directives(source: str) -> list[DartDirective]:
    return parse_directives(get_parser("dart").parse(source.encode()).root_node)


def test_parse_directives_covers_all_forms() -> None:
    source = (
        "import 'package:flutter/material.dart';\n"
        "import 'package:demo/models/car.dart' as car show Car;\n"
        "import '../services/api.dart' hide Foo;\n"
        "export 'src/widgets.dart';\n"
        "part 'home.g.dart';\n"
    )
    assert _directives(source) == [
        DartDirective("import", "package:flutter/material.dart", None, 1),
        DartDirective("import", "package:demo/models/car.dart", "car", 2),
        DartDirective("import", "../services/api.dart", None, 3),
        DartDirective("export", "src/widgets.dart", None, 4),
        DartDirective("part", "home.g.dart", None, 5),
    ]


def test_parse_part_of_uri_and_library_name_forms() -> None:
    assert _directives("part of 'home.dart';\n") == [DartDirective("part_of", "home.dart", None, 1)]
    assert _directives("part of my.lib;\n") == [DartDirective("part_of", None, None, 1)]


def test_package_resolver_maps_own_package_relative_and_rejects_others(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    resolver = DartPackageResolver.from_files(str(tmp_path), ["lib/a/home.dart"])
    assert resolver.packages == {"demo": ""}
    assert resolver.resolve("package:demo/models/car.dart", "lib/a/home.dart") == "lib/models/car.dart"
    assert resolver.resolve("../b/x.dart", "lib/a/home.dart") == "lib/b/x.dart"
    assert resolver.resolve("x.dart", "lib/a/home.dart") == "lib/a/x.dart"
    assert resolver.resolve("package:flutter/material.dart", "lib/a/home.dart") is None
    assert resolver.resolve("dart:async", "lib/a/home.dart") is None
    assert resolver.resolve("../../../escape.dart", "lib/a/home.dart") is None
    assert resolver.resolve(None, "lib/a/home.dart") is None


def test_package_resolver_handles_nested_packages(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: root_app\n", encoding="utf-8")
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "pubspec.yaml").write_text("name: 'core'\n", encoding="utf-8")
    resolver = DartPackageResolver.from_files(
        str(tmp_path), ["lib/main.dart", "packages/core/lib/src/util.dart"]
    )
    assert resolver.packages == {"root_app": "", "core": "packages/core"}
    assert resolver.resolve("package:core/src/util.dart", "lib/main.dart") == (
        "packages/core/lib/src/util.dart"
    )


def test_build_scopes_follows_imports_parts_and_exports() -> None:
    resolver = DartPackageResolver({"demo": ""})
    directives = {
        "lib/home.dart": [
            DartDirective("import", "package:demo/barrel.dart"),
            DartDirective("part", "home_part.dart"),
        ],
        "lib/home_part.dart": [DartDirective("part_of", "home.dart")],
        "lib/barrel.dart": [DartDirective("export", "src/card.dart")],
        "lib/src/card.dart": [DartDirective("export", "deep.dart")],
        "lib/src/deep.dart": [],
        "lib/other.dart": [],
    }
    scopes = build_scopes(directives, resolver, set(directives))
    expected = {
        "lib/home.dart",
        "lib/home_part.dart",
        "lib/barrel.dart",
        "lib/src/card.dart",
        "lib/src/deep.dart",
    }
    assert scopes.visible["lib/home.dart"] == expected
    # A part sees exactly what its library sees.
    assert scopes.visible["lib/home_part.dart"] == expected
    assert scopes.library_members["lib/home_part.dart"] == {"lib/home.dart", "lib/home_part.dart"}
    assert scopes.visible["lib/other.dart"] == {"lib/other.dart"}


def test_part_to_missing_file_is_ignored() -> None:
    resolver = DartPackageResolver({"demo": ""})
    directives = {"lib/home.dart": [DartDirective("part", "home.g.dart")]}
    scopes = build_scopes(directives, resolver, {"lib/home.dart"})
    assert scopes.visible["lib/home.dart"] == {"lib/home.dart"}
    assert scopes.library_members["lib/home.dart"] == {"lib/home.dart"}


def test_export_cycles_terminate() -> None:
    resolver = DartPackageResolver({})
    directives = {
        "a.dart": [DartDirective("import", "b.dart")],
        "b.dart": [DartDirective("export", "c.dart")],
        "c.dart": [DartDirective("export", "b.dart")],
    }
    scopes = build_scopes(directives, resolver, set(directives))
    assert scopes.visible["a.dart"] == {"a.dart", "b.dart", "c.dart"}


def test_is_private_dart_name() -> None:
    assert is_private_dart_name("_Body")
    assert is_private_dart_name("Foo._bar")
    assert not is_private_dart_name("Foo.bar")
    assert not is_private_dart_name("lib/a.dart::_Body")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_dart_imports.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'codewiki.src.be.dependency_analyzer.analyzers.dart_imports'`.

- [ ] **Step 3: Implement** — `codewiki/src/be/dependency_analyzer/analyzers/dart_imports.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `.venv/bin/python -m pytest tests/test_dart_imports.py -q`
Expected: 8 passed. If `test_parse_directives_covers_all_forms` fails on node shapes, dump the source with the Task 0 probe and adjust `parse_directives` only.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check codewiki/src/be/dependency_analyzer/analyzers/dart_imports.py tests/test_dart_imports.py && .venv/bin/ruff format --check codewiki/src/be/dependency_analyzer/analyzers/dart_imports.py tests/test_dart_imports.py`
Expected: no findings (run `ruff format` on the two files if the check fails).

- [ ] **Step 6: Checkpoint** — orchestrator reviews diff; commit message if approved: `dart: parse directives and resolve package/relative URIs`.

---

### Task 2: Dart declarations + pipeline wiring

**Files:**
- Create: `codewiki/src/be/dependency_analyzer/analyzers/dart.py`
- Modify: `codewiki/src/be/dependency_analyzer/analysis/call_graph_analyzer.py` (dispatch block ~line 262, new `_analyze_dart_file` after `_analyze_rust_file` ~line 562, `__init__` ~line 62, `analyze_code_files` reset block ~line 85, viz block ~line 861)
- Modify: `codewiki/src/be/dependency_analyzer/utils/patterns.py`, `codewiki/src/be/dependency_analyzer/ast_parser.py:~191`, `codewiki/src/be/dependency_analyzer/analysis/analysis_service.py` (both language collections, ~lines 346 and 378), `codewiki/cli/utils/repo_validator.py:~36,~68`, `codewiki/cli/utils/validation.py:~170`, `codewiki/mcp/tools/analysis.py:~231`, `codewiki/src/be/prompt_template.py:~355`
- Test: `tests/test_dart_analyzer.py`

**Interfaces:**
- Consumes: `DartDirective`, `parse_directives` (Task 1).
- Produces:
  - `class TreeSitterDartAnalyzer(file_path: str, content: str, repo_path: str | None = None)` with attributes `nodes: list[Node]`, `call_relationships: list[CallRelationship]`, `directives: list[DartDirective]`, `import_prefixes: set[str]`, `top_level_nodes: dict[str, Node]` (logical name → node), property `relative_path: str` (posix).
  - `analyze_dart_file(file_path: str, content: str, repo_path: str | None = None) -> tuple[list[Node], list[CallRelationship]]`
  - Internal state later tasks extend: `_scan_targets: list[_ScanTarget]`, `_field_types: dict[str, dict[str, str]]`, `_field_decls: list[tuple[str, object]]`, `_type_ref_nodes: dict[str, list]`, `_class_extends: dict[str, str | None]`, `_type_params: set[str]`, `_top_level_vars: list[tuple[str, object]]`.
  - `@dataclass class _ScanTarget: caller: str; owner: str | None; node: object; signature: object | None = None; method_name: str | None = None`
  - `CallGraphAnalyzer._dart_directives: dict[str, list[DartDirective]]` (relpath → directives), filled by `_analyze_dart_file`.

- [ ] **Step 1: Write the failing tests** — `tests/test_dart_analyzer.py`:

```python
"""Tests for the tree-sitter based Dart analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.cli.utils.validation import detect_supported_languages
from codewiki.src.be.dependency_analyzer.analyzers.dart import (
    TreeSitterDartAnalyzer,
    analyze_dart_file,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

SAMPLE = """\
import 'package:demo/models/car.dart' as car;

/// Logs messages.
mixin Loggable on Object {
  void log(String m) => print(m);
}

abstract class Repo<T> {
  Future<T> fetch();
}

abstract interface class Disposable {
  void dispose();
}

sealed class Shape {}

/// Cars repo.
class CarRepo extends Repo<Car> with Loggable implements Disposable {
  final ApiClient api;
  CarRepo(this.api);
  factory CarRepo.create() => CarRepo(ApiClient());

  /// Loads the car.
  @override
  Future<Car> fetch() async {
    final r = await api.get('/car');
    log('x');
    return Car.fromJson(r);
  }

  int get count => 1;
  set count(int v) {}

  @override
  void dispose() {}
}

extension StringX on String {
  String shout() => toUpperCase();
}

extension on int {
  int twice() => this * 2;
}

enum Status {
  idle,
  busy;

  bool get isIdle => this == Status.idle;
}

int helper(int a, {int b = 2}) => a + b;

String get appName => 'demo';
"""


def _analyze(tmp_path: Path, source: str = SAMPLE, name: str = "lib/repo.dart"):
    file_path = tmp_path / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(source, encoding="utf-8")
    return analyze_dart_file(str(file_path), source, repo_path=str(tmp_path))


def _by_name(nodes):
    return {node.name: node for node in nodes}


def test_extracts_declarations_with_types(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = _by_name(nodes)
    expected = {
        "Loggable": ("class", "mixin"),
        "Loggable.log": ("method", "method"),
        "Repo": ("class", "abstract class"),
        "Repo.fetch": ("method", "method"),
        "Disposable": ("interface", "interface"),
        "Disposable.dispose": ("method", "method"),
        "Shape": ("class", "sealed class"),
        "CarRepo": ("class", "class"),
        "CarRepo.fetch": ("method", "method"),
        "CarRepo.count": ("method", "getter"),
        "CarRepo.dispose": ("method", "method"),
        "StringX": ("class", "extension"),
        "StringX.shout": ("method", "method"),
        "extension_on_int": ("class", "extension"),
        "extension_on_int.twice": ("method", "method"),
        "Status": ("class", "enum"),
        "Status.isIdle": ("method", "getter"),
        "helper": ("function", "function"),
        "appName": ("function", "getter"),
    }
    assert {name: (n.component_type, n.node_type) for name, n in by_name.items()} == expected
    # Constructors and setters are not components.
    assert "CarRepo.create" not in by_name
    assert all(node.language == "dart" for node in nodes)
    assert by_name["CarRepo"].id == "lib/repo.dart::CarRepo"
    assert by_name["CarRepo.fetch"].class_name == "CarRepo"


def test_bases_docstrings_parameters_and_spans(tmp_path: Path) -> None:
    by_name = _by_name(_analyze(tmp_path)[0])
    assert by_name["CarRepo"].base_classes == ["Repo", "Loggable", "Disposable"]
    assert by_name["Loggable"].base_classes == ["Object"]
    assert by_name["StringX"].base_classes == ["String"]
    assert by_name["CarRepo"].docstring == "/// Cars repo."
    # Doc comment is found across the @override annotation.
    assert by_name["CarRepo.fetch"].docstring == "/// Loads the car."
    assert by_name["helper"].parameters == ["a", "b"]
    fetch = by_name["CarRepo.fetch"]
    assert fetch.source_code.lstrip().startswith("Future<Car> fetch()")
    assert fetch.source_code.rstrip().endswith("}")
    assert fetch.end_line - fetch.start_line == 4


def test_import_prefixes_and_directives(tmp_path: Path) -> None:
    file_path = tmp_path / "lib" / "repo.dart"
    file_path.parent.mkdir(parents=True)
    file_path.write_text(SAMPLE, encoding="utf-8")
    analyzer = TreeSitterDartAnalyzer(str(file_path), SAMPLE, repo_path=str(tmp_path))
    assert analyzer.import_prefixes == {"car"}
    assert analyzer.relative_path == "lib/repo.dart"
    assert [d.kind for d in analyzer.directives] == ["import"]


def test_parse_errors_keep_surrounding_declarations(tmp_path: Path) -> None:
    source = (
        "library;\n"
        "class Before {}\n"
        "extension type CarId(int v) {}\n"
        "Map<String, Object?> body(String? pin) => {'pin': ?pin};\n"
        "class After {\n  void run() {}\n}\n"
    )
    by_name = _by_name(_analyze(tmp_path, source, "lib/broken.dart")[0])
    assert {"Before", "After", "After.run"} <= set(by_name)


def test_empty_and_garbage_files_return_nothing(tmp_path: Path) -> None:
    assert _analyze(tmp_path, "", "lib/empty.dart") == ([], [])
    nodes, _ = _analyze(tmp_path, "class {{{ void (", "lib/garbage.dart")
    assert isinstance(nodes, list)


def test_generated_dart_files_are_ignored(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    (lib / "car.dart").write_text("part 'car.g.dart';\nclass Car {}\n", encoding="utf-8")
    (lib / "car.g.dart").write_text("part of 'car.dart';\nclass _$CarGen {}\n", encoding="utf-8")
    (lib / "car.freezed.dart").write_text("class _$CarFreezed {}\n", encoding="utf-8")
    tool = tmp_path / ".dart_tool"
    tool.mkdir()
    (tool / "gen.dart").write_text("class Hidden {}\n", encoding="utf-8")

    components = DependencyParser(str(tmp_path)).parse_repository()

    dart_ids = {cid for cid, node in components.items() if node.language == "dart"}
    assert dart_ids == {"lib/car.dart::Car"}


def test_dart_is_a_detected_language(tmp_path: Path) -> None:
    (tmp_path / "main.dart").write_text("void main() {}\n", encoding="utf-8")
    assert ("Dart", 1) in detect_supported_languages(tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_dart_analyzer.py -q`
Expected: FAIL — `ModuleNotFoundError: ... analyzers.dart`.

- [ ] **Step 3: Implement `analyzers/dart.py` (declarations only)**

```python
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
            body = following if following is not None and following.type == "function_body" else None
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
                mixins = [_text(t) for t in mixin_node.named_children if t.type == "type_identifier"]
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
            body_node = following if following is not None and following.type == "function_body" else None
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
```

Note for the builder: `test_bases_docstrings_parameters_and_spans` asserts `fetch.end_line - fetch.start_line == 4` (signature line through closing brace; the `@override` and doc lines are excluded because the span starts at `method_signature`). If the grammar puts `documentation_comment` somewhere other than as a preceding sibling, dump with the probe and adjust `_docstring` only.

- [ ] **Step 4: Wire the pipeline** (exact edits)

`utils/patterns.py`:
- `DEFAULT_IGNORE_PATTERNS`: after the `# Java` block add
  ```python
      # Dart/Flutter: tool cache and generated sources
      ".dart_tool",
      ".dart_tool/",
      "*.g.dart",
      "*.freezed.dart",
      "*.gr.dart",
      "*.mocks.dart",
  ```
- `DEFAULT_INCLUDE_PATTERNS`: add `"*.dart",` after `"*.sc",`.
- `CODE_EXTENSIONS`: add `".dart": "dart",` after `".sc": "scala",`.
- `ENTRY_POINT_PATTERNS`: after the Rust block add `    # Dart/Flutter\n    "main.dart",`.
- `FUNCTION_DEFINITION_PATTERNS`: after `"scala": [...]` add
  ```python
      "dart": [
          "class {name}",
          "mixin {name}",
          "extension {name}",
          "enum {name}",
          "void {name}(",
          "Widget {name}(",
          "Future<void> {name}(",
      ],
  ```

`ast_parser.py` extension list: add `".dart",` after `".rs",`.

`analysis/analysis_service.py`: add `"dart",` to the `supported_languages` set (after `"scala",`) and to the list near line 378 (after `"rust",`); update the docstring to "…Kotlin, Scala, Rust, and Dart."

`cli/utils/repo_validator.py`: add `".dart",  # Dart` after `".rs",  # Rust`; message `"Ruby, Scala, Rust, Dart\n\n"`.

`cli/utils/validation.py`: add `"Dart": [".dart"],` after `"Rust": [".rs"],`.

`mcp/tools/analysis.py` `_detect_via_mtime` set: add `".dart",` after `".rs",`.

`cli/main.py` docstring: `Scala, Rust, and Dart.`

`prompt_template.py` `EXTENSION_TO_LANGUAGE`: add `".dart": "dart",` after `".rs": "rust",`.

`analysis/call_graph_analyzer.py`:
- `__init__`: add `self._dart_directives: dict[str, list] = {}`.
- `analyze_code_files`, right after `self._python_external_import_roots = set()`: add `self._dart_directives = {}`.
- dispatch: after the rust branch add
  ```python
                  elif language == "dart":
                      self._analyze_dart_file(file_path, content, repo_dir)
  ```
- after `_analyze_rust_file` add
  ```python
      def _analyze_dart_file(self, file_path: str, content: str, repo_dir: str):
          """
          Analyze Dart file using tree-sitter based analyzer.

          Besides components and relationships, records the file's
          import/export/part directives for Dart scope-aware resolution.

          Args:
              file_path: Path to the Dart file
              content: File content string
              repo_dir: Repository base directory
          """
          from codewiki.src.be.dependency_analyzer.analyzers.dart import TreeSitterDartAnalyzer

          try:
              analyzer = TreeSitterDartAnalyzer(str(file_path), content, repo_path=repo_dir)

              for func in analyzer.nodes:
                  func_id = func.id if func.id else f"{file_path}:{func.name}"
                  self.functions[func_id] = func

              self.call_relationships.extend(analyzer.call_relationships)
              self._dart_directives[analyzer.relative_path] = analyzer.directives
          except Exception:
              logger.exception(f"Failed to analyze Dart file {file_path}")
  ```
- viz block: after the `.rs` branch add `            elif file_ext == ".dart":\n                node_classes.append("lang-dart")`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_dart_analyzer.py tests/test_dart_imports.py -q`
Expected: all pass. If `test_generated_dart_files_are_ignored` fails, inspect how `DependencyParser` matches `DEFAULT_IGNORE_PATTERNS` (basename fnmatch vs path) and adjust only the pattern spelling.

- [ ] **Step 6: Full suite + lint**

Run: `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" tests/ -q` → baseline count + new tests, all pass.
Run: `.venv/bin/ruff check <touched files> && .venv/bin/ruff format --check <touched files>` → clean.

- [ ] **Step 7: Checkpoint** — commit message if approved: `dart: extract declarations and wire .dart through the pipeline`.

---

### Task 3: Intra-file edges (inheritance, field types, calls, instantiation)

**Files:**
- Modify: `codewiki/src/be/dependency_analyzer/analyzers/dart.py`
- Test: `tests/test_dart_analyzer.py` (append)

**Interfaces:**
- Consumes: Task 2 internals (`_scan_targets`, `_field_types`, `_field_decls`, `_type_ref_nodes`, `_type_params`, `top_level_nodes`, `import_prefixes`, `_component_id`).
- Produces (used by Task 5): `_scan(target: _ScanTarget, lift_to: str | None = None)`, `_add_instantiation(caller: str, type_name: str, node, lift_to: str | None)`, `_add_type_edge(caller: str, type_name: str, line: int)`, `_add_resolved(caller: str, callee_id: str, line: int)`, `_add_raw(caller: str, callee: str, line: int)`, hook method `_handle_ref_call(caller: str, member: str, call_node, line: int) -> bool` (returns `False` in this task), `_extract_relationships()` called from `_analyze()` after `_extract_declarations`. Module constants `DART_CORE_TYPES`, `DART_NOISE_CALLS`.

Edge contract (what `call_relationships` contain):
- resolved same-file edges: `callee` = full component id, `is_resolved=True`.
- unresolved: `callee` = bare name (`"ApiClient"`, `"helper"`) or `"Type.member"` (`"ApiClient.get"`), `is_resolved=False`.
- never: self-edges, duplicates `(caller, callee, line)`, names in `DART_CORE_TYPES`/`DART_NOISE_CALLS`, declared type parameters, calls on receivers with unknown type, unresolved `this.x()` calls.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dart_analyzer.py`):

```python
def _edges(relationships):
    return {(r.caller.split("::")[-1], r.callee.split("::")[-1], r.is_resolved) for r in relationships}


def test_inheritance_and_field_type_edges(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    assert ("CarRepo", "Repo", True) in edges
    assert ("CarRepo", "Loggable", True) in edges
    assert ("CarRepo", "Disposable", True) in edges
    assert ("CarRepo", "Car", False) in edges  # type argument of the superclass
    assert ("CarRepo", "ApiClient", False) in edges  # field type
    # Core types and type parameters never become edges.
    callees = {callee for _, callee, _ in edges}
    assert not callees & {"Future", "String", "Object", "int", "T"}


def test_call_and_instantiation_edges(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    assert ("CarRepo.fetch", "ApiClient.get", False) in edges  # typed field receiver
    assert ("CarRepo.fetch", "log", False) in edges  # inherited, resolved cross-file later
    assert ("CarRepo.fetch", "Car", False) in edges  # Car.fromJson -> type edge
    assert ("CarRepo", "CarRepo", True) not in edges  # no self edges
    assert ("CarRepo", "ApiClient", False) in edges  # factory body -> class-level
    assert ("Status.isIdle", "Status", True) in edges  # enum value access
    callees = {callee for _, callee, _ in edges}
    assert "print" not in callees
    assert "toUpperCase" not in callees


CALLS = """\
import 'package:demo/models.dart' as m;

class Engine {
  void start() {}
  void restart() {
    stop();
    this.start();
    start();
  }
  void stop() {}
}

Engine build() => Engine();

class Garage {
  final Engine engine;
  Garage(this.engine);

  void open(Engine spare, {required Door door}) {
    engine.start();
    spare.restart();
    door.unlock();
    final Window w = Window();
    w.close();
    final inferred = Engine();
    inferred.stop();
    engine..start()..stop();
    final box = Box<Item>(1);
    final made = new Crate<int>();
    const label = Label('x');
    final car = m.Car(1);
    final named = Window.tinted();
    build();
    unknown.call();
  }
}
"""


def test_call_resolution_rules(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path, CALLS, "lib/garage.dart")[1])
    expected_present = {
        ("Engine.restart", "Engine.stop", True),
        ("Engine.restart", "Engine.start", True),  # this.start() and bare start()
        ("Garage.open", "Engine.start", True),  # typed field
        ("Garage.open", "Engine.restart", True),  # typed parameter
        ("Garage.open", "Door.unlock", False),  # typed named parameter, unknown type
        ("Garage.open", "Window", False),  # instantiation
        ("Garage.open", "Window.close", False),  # typed local
        ("Garage.open", "Engine", True),
        ("Garage.open", "Engine.stop", True),  # cascade on typed field
        ("Garage.open", "Box", False),  # generic invocation misparse
        ("Garage.open", "Item", False),  # its type argument
        ("Garage.open", "Crate", False),  # new expression
        ("Garage.open", "Label", False),  # const object expression
        ("Garage.open", "Car", False),  # import prefix stripped
        ("Garage.open", "build", True),  # same-file top-level function
        ("build", "Engine", True),
        ("Garage", "Engine", True),  # field type
    }
    assert expected_present <= edges
    callees = {callee for _, callee, _ in edges}
    assert "m" not in callees and "m.Car" not in callees
    assert "unknown.call" not in callees and "call" not in callees
    assert "inferred.stop" not in callees  # untyped local: unknown receiver


def test_relationships_are_deduplicated(tmp_path: Path) -> None:
    source = "class A {}\nclass B {\n  void f() { A(); A(); }\n}\n"
    _, relationships = _analyze(tmp_path, source, "lib/dup.dart")
    keys = [(r.caller, r.callee, r.call_line) for r in relationships]
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_dart_analyzer.py -q -k "edges or rules or deduplicated"`
Expected: FAIL (no relationships emitted yet).

- [ ] **Step 3: Implement** — add to `dart.py`:

Imports at top: `import re`.

Module constants (after `_CLASS_LEVEL_SIGNATURES`):

```python
# Dart core and ubiquitous Flutter framework types never emitted as edges.
DART_CORE_TYPES = frozenset(
    {
        "int", "double", "num", "bool", "String", "Object", "dynamic", "void", "Never",
        "Null", "Function", "Type", "Symbol", "Record", "Enum", "Comparable", "BigInt",
        "List", "Map", "Set", "Iterable", "Iterator", "MapEntry", "Future", "FutureOr",
        "Stream", "StreamController", "StreamSubscription", "Completer", "Timer", "Zone",
        "Duration", "DateTime", "Uri", "RegExp", "StringBuffer", "StackTrace", "Sink",
        "Uint8List", "Exception", "Error", "StateError", "ArgumentError", "RangeError",
        "FormatException", "UnimplementedError", "UnsupportedError",
        "Widget", "BuildContext", "Key", "ValueKey", "GlobalKey", "UniqueKey", "ObjectKey",
        "State", "StatelessWidget", "StatefulWidget", "WidgetRef", "Ref",
    }
)
# Bare calls to SDK/framework functions that never point at a repo component.
DART_NOISE_CALLS = frozenset(
    {
        "print", "debugPrint", "identical", "setState", "jsonEncode", "jsonDecode",
        "unawaited", "max", "min", "runApp", "assert", "scheduleMicrotask",
    }
)
_GENERIC_CALL_RE = re.compile(r"\A\s*([A-Za-z_$][\w$]*)\s*<([^()]*)>\s*\(", re.S)
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _is_type_name(name: str) -> bool:
    stripped = name.lstrip("_$")
    return bool(stripped) and stripped[0].isupper()


def _selector_tokens(selectors) -> list[tuple[str, str | None, object]]:
    """Flatten a selector chain into ("member", name, node) / ("call", None,
    argument_part) / ("other", None, node) tokens."""
    tokens: list[tuple[str, str | None, object]] = []
    for selector in selectors:
        for child in selector.named_children:
            if child.type in ("unconditional_assignable_selector", "conditional_assignable_selector"):
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
```

In `_analyze`, after `self._extract_declarations(root, lines)` add `self._extract_relationships()`.

Pass 2 methods (inside the class):

```python
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
        """Class id that composition edges from this target are also lifted
        to (Flutter build methods). Extended in the Flutter task."""
        return None

    def _scan(self, target, lift_to: str | None = None):
        local_types = _local_declared_types(target.signature, target.node)
        stack = [target.node]
        while stack:
            node = stack.pop()
            if node.type in ("const_object_expression", "new_expression"):
                type_node = _first_child(node, "type_identifier")
                if type_node is not None:
                    self._add_instantiation(target.caller, _text(type_node), node, lift_to)
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
        """Riverpod hook; implemented in the Flutter task."""
        return False

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
```

Builder notes: the `while i < len(children)` loop skips past the consumed selectors so a chain is processed once; the DFS still descends into selector subtrees (arguments contain nested calls). `log('x')` in `CarRepo.fetch` stays unresolved because `log` is inherited from `Loggable` — that is intended (the cross-file resolver handles it).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_dart_analyzer.py tests/test_dart_imports.py -q`
Expected: all pass. For any failing expectation, dump the snippet with the probe before changing code; do not weaken a test without orchestrator approval.

- [ ] **Step 5: Full suite + lint** (as Task 2 Step 6).

- [ ] **Step 6: Checkpoint** — commit message if approved: `dart: emit inheritance, field-type, call and instantiation edges`.

---

### Task 4: Scope-aware cross-file resolution for Dart callers

**Files:**
- Modify: `codewiki/src/be/dependency_analyzer/analysis/call_graph_analyzer.py`
- Test: `tests/test_dart_analyzer.py` (append)

**Interfaces:**
- Consumes: `DartPackageResolver`, `build_scopes`, `DartScopes`, `is_private_dart_name` (Task 1); `self._dart_directives` (Task 2).
- Produces: `CallGraphAnalyzer._dart_visible: dict[str, set[str]]`, `CallGraphAnalyzer._dart_library_members: dict[str, set[str]]`, `_build_dart_scopes(base_dir: str) -> None`, `_resolve_dart_callee(callee: str, caller: Node, lang_indexes: dict | None) -> str | None`, `_dart_scope_candidates(key: str, lang_indexes: dict, scope: set[str]) -> list[str]` (Task 5 extends `_resolve_dart_callee`).

Resolution contract for a Dart caller's unresolved callee:
1. Candidates from the Dart language index (`exact[key]` + `simple[key]`, de-duplicated, order kept) filtered to files in scope; scope = `library_members` for private names, `visible` otherwise. Unique → resolved.
2. For dotted `T.m` with no in-scope `T.m`: same lookup for `T` (class-level edge).
3. Private names that fail 1–2 return `None` and are later dropped as external (never handed to the global unique-name fallback or `ast_parser`'s name fallback, which would bind another library's `_Body`).
4. Public names that fail fall through to the existing global logic unchanged.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dart_analyzer.py`):

```python
def _write_repo(tmp_path: Path, files: dict[str, str]) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    for relpath, source in files.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_imported_definition_wins_over_global_duplicate(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/a/home.dart": (
                "import 'package:demo/a/widgets.dart';\n"
                "class HomePage {\n  void show() { InfoCard(); }\n}\n"
            ),
            "lib/a/widgets.dart": "class InfoCard {}\n",
            "lib/b/other.dart": "class InfoCard {}\n",
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    deps = components["lib/a/home.dart::HomePage.show"].depends_on
    assert "lib/a/widgets.dart::InfoCard" in deps
    assert "lib/b/other.dart::InfoCard" not in deps


def test_private_names_bind_within_library_only(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/c/lib.dart": "part 'part.dart';\nclass X {\n  void f() { _helper(); }\n}\n",
            "lib/c/part.dart": "part of 'lib.dart';\nvoid _helper() {}\nvoid _onlyInC() {}\n",
            "lib/d/z.dart": (
                "void _helper() {}\n"
                "class Z {\n  void g() { _helper(); _onlyInC(); }\n}\n"
            ),
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert components["lib/c/lib.dart::X.f"].depends_on == {"lib/c/part.dart::_helper"}
    # Z.g sees its own _helper; _onlyInC is private to library c and must not bind.
    assert components["lib/d/z.dart::Z.g"].depends_on == {"lib/d/z.dart::_helper"}


def test_dotted_callee_falls_back_to_visible_type(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/api.dart": "class ApiClient {\n  void get() {}\n}\n",
            "lib/other_api.dart": "class Other {\n  void get() {}\n}\n",
            "lib/repo.dart": (
                "import 'api.dart';\n"
                "class Repo {\n  final ApiClient api;\n  Repo(this.api);\n"
                "  void load() { api.get(); api.post(); }\n}\n"
            ),
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    deps = components["lib/repo.dart::Repo.load"].depends_on
    assert "lib/api.dart::ApiClient.get" in deps
    assert "lib/api.dart::ApiClient" in deps  # api.post(): no such method -> the type
    assert "lib/other_api.dart::Other.get" not in deps


def test_inherited_mixin_call_resolves_cross_file(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/log.dart": "mixin Loggable {\n  void log(String m) {}\n}\n",
            "lib/svc.dart": (
                "import 'log.dart';\n"
                "class Svc with Loggable {\n  void run() { log('x'); }\n}\n"
            ),
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert "lib/log.dart::Loggable.log" in components["lib/svc.dart::Svc.run"].depends_on
    assert "lib/log.dart::Loggable" in components["lib/svc.dart::Svc"].depends_on
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_dart_analyzer.py -q -k "duplicate or private or fallback or mixin_call"`
Expected: `test_imported_definition_wins_over_global_duplicate` and `test_private_names_bind_within_library_only` FAIL (ambiguous names); the other two may already pass — that is fine, they guard regressions.

- [ ] **Step 3: Implement** in `call_graph_analyzer.py`:

`__init__` and the `analyze_code_files` reset block: add

```python
        self._dart_visible: dict[str, set[str]] = {}
        self._dart_library_members: dict[str, set[str]] = {}
```

(in `analyze_code_files` reset them to `{}` next to `self._dart_directives = {}`).

In `analyze_code_files`, immediately before `logger.debug("Resolving call relationships")`:

```python
        if self._dart_directives:
            self._build_dart_scopes(base_dir)
```

New methods (place after `_analyze_dart_file`):

```python
    def _build_dart_scopes(self, base_dir: str) -> None:
        """Resolve recorded Dart directives into per-file visibility scopes."""
        from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
            DartPackageResolver,
            build_scopes,
        )

        files = set(self._dart_directives)
        resolver = DartPackageResolver.from_files(base_dir, files)
        scopes = build_scopes(self._dart_directives, resolver, files)
        self._dart_visible = scopes.visible
        self._dart_library_members = scopes.library_members

    def _dart_scope_candidates(self, key: str, lang_indexes: dict, scope: set[str]) -> list[str]:
        candidates: list[str] = []
        for func_id in [*lang_indexes["exact"].get(key, []), *lang_indexes["simple"].get(key, [])]:
            func = self.functions.get(func_id)
            if func_id in candidates or func is None:
                continue
            if Path(func.relative_path).as_posix() in scope:
                candidates.append(func_id)
        return candidates

    def _resolve_dart_callee(
        self, callee: str, caller: Node, lang_indexes: dict | None
    ) -> str | None:
        """Prefer definitions the caller's library can see (imports, parts,
        re-exports); library-private names only resolve inside the library."""
        from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
            is_private_dart_name,
        )

        if not lang_indexes or "::" in callee:
            return None
        caller_file = Path(caller.relative_path).as_posix()
        if is_private_dart_name(callee):
            scope = self._dart_library_members.get(caller_file, {caller_file})
        else:
            scope = self._dart_visible.get(caller_file)
        if not scope:
            return None
        keys = [callee]
        if "." in callee:
            keys.append(callee.split(".")[0])
        for key in keys:
            candidates = self._dart_scope_candidates(key, lang_indexes, scope)
            if len(candidates) == 1:
                return candidates[0]
        return None
```

In `_resolve_callee`, after `lang_indexes = ...` and before the existing `if lang_indexes:` block:

```python
        if caller_language == "dart" and caller is not None:
            from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
                is_private_dart_name,
            )

            match = self._resolve_dart_callee(relationship.callee, caller, lang_indexes)
            if match:
                return match
            if is_private_dart_name(relationship.callee):
                return None
```

In `_is_external_callee`, before the final `return False`:

```python
        if language == "dart":
            from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
                is_private_dart_name,
            )

            # A library-private name that did not resolve inside its library
            # can never be a component elsewhere; dropping it keeps the
            # name-based fallback from binding another library's `_Body`.
            if is_private_dart_name(callee):
                return True
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_dart_analyzer.py tests/test_dart_imports.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite + lint** — the full suite proves non-Dart resolution is unchanged.

- [ ] **Step 6: Checkpoint** — commit message if approved: `dart: resolve callees through imports, parts and exports`.

---

### Task 5: Flutter awareness (widgets, build() composition, Riverpod, GoRouter)

**Files:**
- Create: `codewiki/src/be/dependency_analyzer/analyzers/dart_flutter.py`
- Modify: `codewiki/src/be/dependency_analyzer/analyzers/dart.py`, `codewiki/src/be/dependency_analyzer/analysis/call_graph_analyzer.py` (`_resolve_dart_callee`)
- Test: `tests/test_dart_flutter.py`

**Interfaces:**
- Consumes: Task 2/3 internals (`_class_extends`, `_top_level_vars`, `_scan_targets`, `_lift_target`, `_handle_ref_call`, `_add_node`, `_add_resolved`, `_add_raw`, `top_level_nodes`), Task 4 `_resolve_dart_callee`, `_dart_scope_candidates`.
- Produces (`dart_flutter.py`):
  - `FLUTTER_WIDGET_BASES: frozenset[str]`, `FLUTTER_STATE_BASES: frozenset[str]`, `STATE_MANAGEMENT_BASES: dict[str, str]`, `RIVERPOD_ANNOTATIONS: frozenset[str]`, `REF_METHODS: frozenset[str]`, `COMPOSITION_METHOD_RE: re.Pattern`
  - `classify_class(extends: str | None, local_widgets: set[str]) -> str | None` → `"widget" | "state" | "notifier" | "bloc" | "cubit" | None`
  - `classify_top_level_initializer(text: str) -> str | None` → `"provider" | "router" | None`
  - `provider_alias_candidates(callee: str) -> list[str]` (`"counterProvider"` → `["counter", "Counter", "CounterNotifier"]`; non-`…Provider` → `[]`)
- Node types after this task: widget classes `node_type="widget"`, State classes `"state"`, notifiers `"notifier"`, `@riverpod` classes `"notifier"`, `@riverpod` functions `"provider"`, top-level provider variables `component_type="function", node_type="provider"`, `GoRouter` variables `component_type="function", node_type="router"`.

- [ ] **Step 1: Write the failing tests** — `tests/test_dart_flutter.py`:

```python
"""Tests for the Flutter-aware parts of the Dart analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analyzers.dart import analyze_dart_file
from codewiki.src.be.dependency_analyzer.analyzers.dart_flutter import (
    classify_class,
    classify_top_level_initializer,
    provider_alias_candidates,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

APP = """\
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

final carProvider = FutureProvider<Car>((ref) => ref.watch(repoProvider).fetch());
final counterProvider = NotifierProvider<CounterNotifier, int>(CounterNotifier.new);
final router = GoRouter(routes: [
  GoRoute(path: '/', builder: (c, s) => const HomePage()),
  GoRoute(path: '/settings', builder: (c, s) => SettingsPage()),
]);
final greeting = 'hi';

class CounterNotifier extends Notifier<int> {
  @override
  int build() => 0;
}

class HomePage extends ConsumerWidget {
  const HomePage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final car = ref.watch(carProvider);
    final n = ref.watch(counterProvider.notifier);
    return Scaffold(
      body: Column(children: [CarCard(car: car), BatteryGauge.small(), _buildFooter()]),
    );
  }

  Widget _buildFooter() => const Footer();
}

class CarCard extends StatelessWidget {
  Widget build(BuildContext context) => Text('car');
}

class Counter extends StatefulWidget {
  @override
  State<Counter> createState() => _CounterState();
}

class _CounterState extends State<Counter> {
  @override
  Widget build(BuildContext context) => CarCard();
}

class FancyCard extends CarCard {}
"""


def _analyze(tmp_path: Path, source: str = APP, name: str = "lib/app.dart"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return analyze_dart_file(str(path), source, repo_path=str(tmp_path))


def _edges(relationships):
    return {(r.caller.split("::")[-1], r.callee.split("::")[-1]) for r in relationships}


def test_classifiers() -> None:
    assert classify_class("StatelessWidget", set()) == "widget"
    assert classify_class("ConsumerStatefulWidget", set()) == "widget"
    assert classify_class("CarCard", {"CarCard"}) == "widget"
    assert classify_class("State", set()) == "state"
    assert classify_class("ConsumerState", set()) == "state"
    assert classify_class("Notifier", set()) == "notifier"
    assert classify_class("_$Counter", set()) == "notifier"
    assert classify_class("Cubit", set()) == "cubit"
    assert classify_class("Repo", set()) is None
    assert classify_class(None, set()) is None
    assert classify_top_level_initializer(" FutureProvider<Car>((ref) => 1)") == "provider"
    assert classify_top_level_initializer(" StateProvider.autoDispose((ref) => 0)") == "provider"
    assert classify_top_level_initializer(" GoRouter(routes: [])") == "router"
    assert classify_top_level_initializer(" 'hi'") is None
    assert provider_alias_candidates("cartProvider") == ["cart", "Cart", "CartNotifier"]
    assert provider_alias_candidates("Provider") == []
    assert provider_alias_candidates("cart") == []


def test_flutter_node_types(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    types = {n.name: (n.component_type, n.node_type) for n in nodes}
    assert types["HomePage"] == ("class", "widget")
    assert types["CarCard"] == ("class", "widget")
    assert types["FancyCard"] == ("class", "widget")  # same-file widget subclass
    assert types["Counter"] == ("class", "widget")
    assert types["_CounterState"] == ("class", "state")
    assert types["CounterNotifier"] == ("class", "notifier")
    assert types["carProvider"] == ("function", "provider")
    assert types["counterProvider"] == ("function", "provider")
    assert types["router"] == ("function", "router")
    assert "greeting" not in types
    assert next(n for n in nodes if n.name == "HomePage").display_name == "widget HomePage"


def test_build_composition_is_lifted_to_widget_class(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    for child in ("CarCard", "BatteryGauge", "Footer", "Scaffold", "Column"):
        assert ("HomePage", child) in edges, child
    assert ("HomePage.build", "CarCard") in edges  # method-level edge kept
    assert ("Counter", "_CounterState") in edges  # createState pairing
    assert ("_CounterState", "Counter") in edges  # State<Counter> type argument
    assert ("_CounterState", "CarCard") in edges


def test_riverpod_and_router_edges(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    assert ("HomePage.build", "carProvider") in edges
    assert ("HomePage.build", "counterProvider") in edges  # .notifier stripped
    assert ("carProvider", "repoProvider") in edges
    assert ("carProvider", "Car") in edges
    assert ("counterProvider", "CounterNotifier") in edges  # type arg and tear-off
    assert ("router", "HomePage") in edges
    assert ("router", "SettingsPage") in edges
    callees = {callee for _, callee in edges}
    assert "ref.watch" not in callees and "watch" not in callees


def test_riverpod_codegen_alias_resolves(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "state.dart").write_text(
        "import 'package:riverpod_annotation/riverpod_annotation.dart';\n"
        "part 'state.g.dart';\n"
        "@riverpod\nclass Counter extends _$Counter {\n  @override\n  int build() => 0;\n}\n"
        "@riverpod\nclass CartNotifier extends _$CartNotifier {\n  int build() => 0;\n}\n"
        "@Riverpod(keepAlive: true)\nFuture<int> total(Ref ref) async => 1;\n"
        "Future<int> notAProvider() async => 1;\n",
        encoding="utf-8",
    )
    (lib / "state.g.dart").write_text("part of 'state.dart';\n", encoding="utf-8")
    (lib / "view.dart").write_text(
        "import 'state.dart';\n"
        "class View extends ConsumerWidget {\n"
        "  Widget build(BuildContext context, WidgetRef ref) {\n"
        "    ref.watch(counterProvider);\n"
        "    ref.watch(totalProvider);\n"
        "    ref.watch(cartProvider);\n"          # Riverpod 3 naming
        "    ref.read(cartNotifierProvider);\n"   # Riverpod 2 naming
        "    ref.watch(notAProviderProvider);\n"
        "    return const Placeholder();\n  }\n}\n",
        encoding="utf-8",
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert components["lib/state.dart::Counter"].node_type == "notifier"
    assert components["lib/state.dart::total"].node_type == "provider"
    deps = components["lib/view.dart::View.build"].depends_on
    assert {
        "lib/state.dart::Counter",
        "lib/state.dart::total",
        "lib/state.dart::CartNotifier",
    } <= deps
    # Only @riverpod-annotated declarations are alias targets.
    assert "lib/state.dart::notAProvider" not in deps
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_dart_flutter.py -q`
Expected: FAIL — `ModuleNotFoundError: ... dart_flutter`.

- [ ] **Step 3: Implement `dart_flutter.py`**

```python
"""Flutter, Riverpod, Bloc and GoRouter knowledge for the Dart analyzer.

Pure tables and classifiers; the analyzer decides where to apply them.
Classification is by declared supertype name, so only direct framework
subclasses and same-file subclass chains are recognised.
"""

import re

FLUTTER_WIDGET_BASES = frozenset(
    {
        "StatelessWidget",
        "StatefulWidget",
        "ConsumerWidget",
        "ConsumerStatefulWidget",
        "HookWidget",
        "HookConsumerWidget",
        "StatefulHookWidget",
        "StatefulHookConsumerWidget",
        "InheritedWidget",
        "InheritedNotifier",
        "InheritedModel",
        "RenderObjectWidget",
        "LeafRenderObjectWidget",
        "SingleChildRenderObjectWidget",
        "MultiChildRenderObjectWidget",
        "ProxyWidget",
        "ParentDataWidget",
    }
)
FLUTTER_STATE_BASES = frozenset({"State", "ConsumerState"})
STATE_MANAGEMENT_BASES = {
    "ChangeNotifier": "notifier",
    "ValueNotifier": "notifier",
    "Notifier": "notifier",
    "AsyncNotifier": "notifier",
    "StreamNotifier": "notifier",
    "FamilyNotifier": "notifier",
    "FamilyAsyncNotifier": "notifier",
    "AutoDisposeNotifier": "notifier",
    "AutoDisposeAsyncNotifier": "notifier",
    "StateNotifier": "notifier",
    "Bloc": "bloc",
    "HydratedBloc": "bloc",
    "Cubit": "cubit",
    "HydratedCubit": "cubit",
}
RIVERPOD_ANNOTATIONS = frozenset({"riverpod", "Riverpod"})
REF_METHODS = frozenset({"watch", "read", "listen", "listenManual", "invalidate", "refresh", "exists"})
# Methods whose instantiations are the widget's composition: build(),
# helper builders (buildHeader, _buildRow) and createState().
COMPOSITION_METHOD_RE = re.compile(r"^(_?build\w*|createState)$")
_INITIALIZER_CTOR_RE = re.compile(r"\A\s*(?:const\s+|new\s+)?([A-Z]\w*)(?:\.\w+)*\s*(?:<[^()]*>)?\s*\(", re.S)
_PROVIDER_SUFFIX = "Provider"


def classify_class(extends: str | None, local_widgets: set[str]) -> str | None:
    if not extends:
        return None
    if extends.startswith("_$"):
        return "notifier"  # riverpod_generator base class
    if extends in FLUTTER_WIDGET_BASES or extends in local_widgets:
        return "widget"
    if extends in FLUTTER_STATE_BASES:
        return "state"
    return STATE_MANAGEMENT_BASES.get(extends)


def classify_top_level_initializer(text: str) -> str | None:
    match = _INITIALIZER_CTOR_RE.match(text)
    if not match:
        return None
    constructor = match.group(1)
    if constructor.endswith(_PROVIDER_SUFFIX):
        return "provider"
    if constructor == "GoRouter":
        return "router"
    return None


def provider_alias_candidates(callee: str) -> list[str]:
    """Declarations riverpod_generator may have generated ``callee`` from:
    ``fooProvider`` comes from function ``foo`` or class ``Foo`` (Riverpod 2
    and 3), or class ``FooNotifier`` (Riverpod 3 drops the suffix)."""
    if not callee.endswith(_PROVIDER_SUFFIX) or "." in callee:
        return []
    stem = callee[: -len(_PROVIDER_SUFFIX)]
    if not stem:
        return []
    upper = stem[:1].upper() + stem[1:]
    return [stem, upper, f"{upper}Notifier"]
```

- [ ] **Step 4: Integrate into `dart.py`**

Imports:

```python
from codewiki.src.be.dependency_analyzer.analyzers.dart_flutter import (
    COMPOSITION_METHOD_RE,
    REF_METHODS,
    RIVERPOD_ANNOTATIONS,
    classify_class,
    classify_top_level_initializer,
)
```

Helper (module level):

```python
def _has_riverpod_annotation(annotations) -> bool:
    return any(_text(a.child_by_field_name("name")) in RIVERPOD_ANNOTATIONS for a in annotations)
```

In `_analyze`, between `self._extract_declarations(root, lines)` and `self._extract_relationships()` insert `self._apply_flutter_kinds(root, lines)`.

New methods:

```python
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
                    self._set_node_type(_text(child.child_by_field_name("name")), "notifier")
            elif child.type in _TOP_LEVEL_SIGNATURES:
                annotations = []
                sibling = child.prev_named_sibling
                while sibling is not None and sibling.type in ("annotation", "documentation_comment", "comment"):
                    if sibling.type == "annotation":
                        annotations.append(sibling)
                    sibling = sibling.prev_named_sibling
                if _has_riverpod_annotation(annotations):
                    self._set_node_type(_text(child.child_by_field_name("name")), "provider")

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
```

Replace the Task 3 stubs:

```python
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

    def _handle_ref_call(self, caller: str, member: str, call_node, line: int) -> bool:
        """`ref.watch(fooProvider)` / `.read` / `.listen` / ...: an edge to the
        provider (`.notifier`, `.future` and family arguments stripped)."""
        if member not in REF_METHODS:
            return False
        arguments = _first_child(call_node, "arguments")
        first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
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
```

Builder note: if `ref.watch(counterProvider.notifier)` yields an argument whose first named child is not the bare identifier, dump it with the probe and unwrap one more level; do not loosen the `endswith("Provider")` guard.

- [ ] **Step 5: Extend `_resolve_dart_callee`** in `call_graph_analyzer.py` — before its final `return None`:

```python
        from codewiki.src.be.dependency_analyzer.analyzers.dart_flutter import (
            provider_alias_candidates,
        )

        # riverpod_generator providers live in ignored *.g.dart files: map
        # `fooProvider` to the @riverpod declaration it was generated from.
        for alias in provider_alias_candidates(callee):
            candidates = [
                func_id
                for func_id in self._dart_scope_candidates(alias, lang_indexes, scope)
                if self.functions[func_id].node_type in ("provider", "notifier")
                and self.functions[func_id].name == alias
            ]
            if len(candidates) == 1:
                return candidates[0]
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_dart_flutter.py tests/test_dart_analyzer.py tests/test_dart_imports.py -q`
Expected: all pass. Note Task 2's `test_extracts_declarations_with_types` must still pass (SAMPLE has no Flutter bases).

- [ ] **Step 7: Full suite + lint.**

- [ ] **Step 8: Checkpoint** — commit message if approved: `dart: Flutter widget composition, Riverpod and GoRouter edges`.

---

### Task 6: Flutter-aware documentation prompt hint

**Files:**
- Modify: `codewiki/src/be/prompt_template.py` (constant near `ARTIFACT_USAGE_NOTE` ~line 315; `format_user_prompt` `_assemble` ~line 573)
- Test: `tests/test_doc_layout.py` (append)

**Interfaces:**
- Consumes: Node fields `relative_path`, `file_path`.
- Produces: `DART_FLUTTER_NOTE: str`; `format_user_prompt` appends it when any core component's `relative_path` ends with `.dart`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_doc_layout.py`; add `DART_FLUTTER_NOTE` to the existing `from codewiki.src.be.prompt_template import (...)` block and `from codewiki.src.be.dependency_analyzer.models.core import Node` to the imports):

```python
def _component(tmp_path, relpath: str, cid: str) -> Node:
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// code\n", encoding="utf-8")
    return Node(
        id=cid,
        name=cid.split("::")[-1],
        component_type="class",
        file_path=str(path),
        relative_path=relpath,
    )


def test_dart_modules_get_flutter_note(tmp_path):
    components = {"lib/home.dart::HomePage": _component(tmp_path, "lib/home.dart", "lib/home.dart::HomePage")}
    prompt = format_user_prompt("auth", list(components), components, TREE, ["auth"], "flat")
    assert DART_FLUTTER_NOTE in prompt
    assert "```dart" in prompt


def test_non_dart_modules_have_no_flutter_note(tmp_path):
    components = {"a.py::A": _component(tmp_path, "a.py", "a.py::A")}
    prompt = format_user_prompt("auth", list(components), components, TREE, ["auth"], "flat")
    assert DART_FLUTTER_NOTE not in prompt
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_doc_layout.py -q -k flutter_note`
Expected: FAIL — `ImportError: cannot import name 'DART_FLUTTER_NOTE'`.

- [ ] **Step 3: Implement** in `prompt_template.py`, after `ARTIFACT_USAGE_NOTE`:

```python
# Appended to the user prompt when the module contains Dart sources. Kept
# out of USER_PROMPT for the same reason as ARTIFACT_USAGE_NOTE.
DART_FLUTTER_NOTE = (
    "* NOTE (Dart/Flutter): when these components are Flutter code, also cover, "
    "where they apply: (1) the widget tree — which widgets each widget builds "
    "and where it is used, and Stateless vs Stateful/Consumer widgets; (2) state "
    "management — where state lives (State classes, Riverpod providers and "
    "notifiers, Bloc/Cubit, ChangeNotifier), which providers each widget "
    "watches or reads, and how updates flow to the UI; (3) navigation — routes, "
    "paths and screens (GoRouter, Navigator pushes) and how parameters are "
    "passed; (4) lifecycle and async behaviour (initState/dispose, Futures, "
    "Streams). Leave out any of these that do not apply."
)
```

In `format_user_prompt`, after `artifact_section = ...`:

```python
    has_dart = any(
        components[cid].relative_path.endswith(".dart")
        for cid in core_component_ids
        if cid in components
    )
    dart_section = f"\n\n{DART_FLUTTER_NOTE}" if has_dart else ""
```

and in `_assemble`, after `+ artifact_section` add `+ dart_section`.

- [ ] **Step 4: Run tests** — `.venv/bin/python -m pytest tests/test_doc_layout.py -q` → all pass.

- [ ] **Step 5: Full suite + lint.**

- [ ] **Step 6: Checkpoint** — commit message if approved: `prompts: add Flutter-aware documentation hints for Dart modules`.

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (Features → Languages), `CHANGELOG.md` (`## [Unreleased]` → `### Added`), `guides/development.md` (Tests bullet)

- [ ] **Step 1: README** — languages line becomes `PHP, Ruby, Scala, Rust, Dart (Flutter-aware).`
- [ ] **Step 2: CHANGELOG** — under the existing `## [Unreleased]` / `### Added`, append:

```markdown
- **Dart / Flutter analyzer.** Classes, mixins, extensions, enums, top-level
  functions and getters, and methods become components, with inheritance,
  field-type, instantiation and call edges. `package:`/relative imports,
  exports and `part` files decide which definition a name resolves to, and
  library-private names never resolve outside their library. Flutter-aware:
  widgets and State classes are typed, instantiations in `build()` become
  widget-to-widget edges, `ref.watch`/`ref.read` link widgets to Riverpod
  providers (including `@riverpod` codegen), and GoRouter routes link to their
  screens. Generated `*.g.dart`/`*.freezed.dart` files and `.dart_tool/` are
  ignored, and Dart modules get Flutter-specific documentation guidance.
```

- [ ] **Step 3: guides/development.md** — test list becomes `` `tests/test_scala_analyzer.py`, `tests/test_ruby_analyzer.py`, `tests/test_rust_analyzer.py`, and `tests/test_dart_analyzer.py` ``.
- [ ] **Step 4: Checkpoint** — commit message if approved: `docs: document Dart/Flutter support`.

---

### Task 8: Analysis-only validation on avto-consumption (tester agent)

**Files:**
- Create: `$SCRATCH/analyze_only.py` (not in repo)

- [ ] **Step 1: Write the harness**

```python
"""Analysis-only CodeWiki run: dependency graph, no LLM.

Usage: .venv/bin/python analyze_only.py <repo_dir> <out_dir>
Writes graph + metrics.json under <out_dir>; never writes into <repo_dir>.
"""

import json
import logging
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
from codewiki.src.config import Config

CLASS_RE = re.compile(
    r"^(?:abstract |sealed |base |interface |final |mixin )*(?:class|mixin|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
GENERATED = (".g.dart", ".freezed.dart", ".gr.dart", ".mocks.dart")


class _ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(self.format(record)[:200])


def main(repo: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    errors = _ErrorCounter()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger().addHandler(errors)
    config = Config(
        repo_path=str(repo),
        output_dir=str(out / "temp"),
        dependency_graph_dir=str(out / "temp" / "dependency_graphs"),
        docs_dir=str(out),
        max_depth=2,
        llm_base_url="not-needed",
        llm_api_key="not-needed",
        main_model="unused",
        cluster_model="unused",
        use_gitignore=True,
    )
    started = time.time()
    components, leaves = DependencyGraphBuilder(config).build_dependency_graph()
    elapsed = time.time() - started

    dart = {cid: n for cid, n in components.items() if n.language == "dart"}
    by_type = Counter(n.node_type for n in dart.values())
    edges = [(cid, dep) for cid, n in dart.items() for dep in n.depends_on]
    incoming = Counter(dep for _, dep in edges)

    def kind(cid):
        node = components.get(cid)
        return node.node_type if node is not None else None

    ui = ("widget", "state")
    composition = [(a, b) for a, b in edges if kind(a) in ui and kind(b) in ui]
    widgets = [cid for cid, n in dart.items() if n.node_type in ui]
    widgets_with_children = {a for a, _ in composition}
    provider_edges = [(a, b) for a, b in edges if kind(b) in ("provider", "notifier")]
    classes = [cid for cid, n in dart.items() if n.component_type in ("class", "interface")]
    orphans = [cid for cid in classes if not dart[cid].depends_on and not incoming[cid]]

    expected = set()
    for path in (repo / "lib").rglob("*.dart"):
        if path.name.endswith(GENERATED):
            continue
        rel = path.relative_to(repo).as_posix()
        for name in CLASS_RE.findall(path.read_text(encoding="utf-8", errors="replace")):
            expected.add(f"{rel}::{name}")
    found = {cid for cid in classes if cid in expected}
    missing = sorted(expected - found)

    random.seed(7)
    samples = random.sample(edges, min(10, len(edges)))
    metrics = {
        "seconds": round(elapsed, 1),
        "logged_errors": len(errors.records),
        "dart_files": len({n.relative_path for n in dart.values()}),
        "dart_components": len(dart),
        "by_node_type": dict(by_type),
        "dart_edges": len(edges),
        "composition_edges": len(composition),
        "widgets": len(widgets),
        "widgets_with_child_widgets_pct": round(100 * len(widgets_with_children) / max(1, len(widgets)), 1),
        "provider_edges": len(provider_edges),
        "orphan_classes": len(orphans),
        "class_coverage_pct": round(100 * len(found) / max(1, len(expected)), 1),
        "missing_classes": missing[:30],
        "leaf_nodes": len(leaves),
        "sample_edges": samples,
        "error_samples": errors.records[:10],
        "top_fan_in": incoming.most_common(10),
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser().resolve(), Path(sys.argv[2]).expanduser().resolve())
```

- [ ] **Step 2: Run on avto-consumption**

Run: `cd ~/Documents/Github/CodeWiki && .venv/bin/python $SCRATCH/analyze_only.py ~/Documents/Github/avto-consumption/app $SCRATCH/runs/avto`
Expected: completes; `git -C ~/Documents/Github/avto-consumption status --short` unchanged before/after.

- [ ] **Step 3: Check acceptance** (tester reports each with the number):
  - `logged_errors == 0` (otherwise list `error_samples`).
  - `class_coverage_pct >= 95` (list `missing_classes` and the parse-error cause for each).
  - `widgets_with_child_widgets_pct >= 60` (leaf widgets that only build framework widgets legitimately have none; report the figure and five widgets without children with a one-line reason each).
  - `provider_edges > 0` (avto uses hand-written Riverpod providers).
  - `seconds < 60`.
  - Manual spot check of the 10 `sample_edges`: open both ends; ≥ 9 must be real dependencies. Report each verdict.
- [ ] **Step 4: Fix loop** — any failed criterion goes back to the orchestrator as a bug with evidence; the orchestrator assigns a builder with a brief that adds a failing test first (superpowers:systematic-debugging), then re-runs Steps 2–3.

---

### Task 9: Final check on NorthStar + whole-branch review

- [ ] **Step 1: Run**: `.venv/bin/python $SCRATCH/analyze_only.py ~/Documents/Github/NorthStar/app $SCRATCH/runs/northstar` (NorthStar repo untouched — verify with `git status`).
- [ ] **Step 2: Acceptance**: no crash, `logged_errors == 0`, `class_coverage_pct >= 95`, `seconds < 300`, `provider_edges > 0` with at least one edge whose target was resolved through the `@riverpod` alias (tester greps one `ref.watch(xProvider)` in `lib/` and confirms its edge in `$SCRATCH/runs/northstar/temp/dependency_graphs/*.json`).
- [ ] **Step 3: Full suite + ruff** over every touched file (CI parity): `.venv/bin/python -m pytest -p no:cacheprovider -o addopts="" tests/ -q`, `.venv/bin/ruff check codewiki tests`, `.venv/bin/ruff format --check <touched files>`.
- [ ] **Step 4: Whole-branch review** (opus reviewer, superpowers:requesting-code-review) against this plan and the Review Focus list.
- [ ] **Step 5: Paid run (user-approved, one run)**: `codewiki generate` on `~/Documents/Github/avto-consumption/app` with `--output $SCRATCH/runs/avto-docs`. Orchestrator judges the output: if it is a real explanation of the app (correct screens, providers, routing), copy it into the avto-consumption repo (`app/docs/codewiki/`, uncommitted — the user commits there); otherwise leave it in `$SCRATCH/runs/avto-docs` and report why.
- [ ] **Step 6: Hand-off** — summarize metrics for both repos. **No push, no PR** — the user pushes to the fork and opens the PR themselves.
