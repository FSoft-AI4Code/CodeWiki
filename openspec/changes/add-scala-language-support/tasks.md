## 1. Grammar Spike

- [ ] 1.1 Add `tree-sitter-scala>=0.26.2` to `pyproject.toml` and pin `tree-sitter-scala==0.26.2` in `requirements.txt`; verify `pip install -e .` succeeds and `python -c "import tree_sitter_scala"` imports without a source build
- [ ] 1.2 Parse a representative Scala 2 file and a Scala 3 file using significant-indentation syntax, `enum`, `given`, and `extension`; verify neither tree contains `ERROR` or `MISSING` nodes. If Scala 3 indentation fails, stop and revisit the design before continuing — this gates the extraction approach
- [ ] 1.3 Record the concrete grammar node type names for class, case class, trait, object, package object, enum, method, and top-level definitions; verify each name appears in the grammar's node types rather than being carried over from the Kotlin analyzer

## 2. Analyzer Core

- [ ] 2.1 Create `codewiki/src/be/dependency_analyzer/analyzers/scala.py` with a `TreeSitterScalaAnalyzer` class and an `analyze_scala_file(file_path, content, repo_path)` entry point mirroring the Kotlin analyzer's structure; verify it returns empty node and relationship lists for an empty file without raising
- [ ] 2.2 Implement module-path and relative-path derivation stripping `.scala` and `.sc`; verify a nested file yields a dotted module path with the extension removed
- [ ] 2.3 Implement component ID construction, giving companion objects the  `suffix per design Decision 2; verify a file declaring both` class Buffer `and` object Buffer `yields` buffer.scala::Buffer `and` buffer.scala::Buffer as distinct components with neither displaced
- [ ] 2.4 Extract classes, case classes, traits, objects, package objects, and Scala 3 enums, applying the type mapping from design Decision 1; verify a trait is emitted with component type `interface` and an object with `class`
- [ ] 2.5 Extract methods with their enclosing type as owner, and top-level definitions as free functions; verify a method inside a trait is attributed to that trait and a top-level `def` is emitted as a function
- [ ] 2.6 Extract parameters, source ranges, and Scaladoc or line-comment documentation for each component; verify a documented method reports its parameter list and a non-empty docstring

## 3. Relationship Extraction

- [ ] 3.1 Emit inheritance and trait-mixin edges from `extends`/`with` clauses; verify a class extending a repository-local base class and mixing in a repository-local trait produces a resolved edge for each
- [ ] 3.2 Emit edges for constructor parameter types, field types, and instantiation; verify a class with a field of a repository-local type produces a resolved edge to it
- [ ] 3.3 Emit method-call edges, resolving intra-file targets to sibling components and leaving external targets as unresolved logical names; verify an intra-type call resolves and a standard-library call does not
- [ ] 3.4 Define a Scala primitive and common built-in exclusion set following the Kotlin and PHP precedent; verify references to primitive types and ubiquitous collection operations produce no edges

## 4. Pipeline Registration

- [ ] 4.1 Add `"scala"` to both language sets in `analysis/analysis_service.py` (`_filter_supported_languages` and `_get_supported_languages`); verify Scala files are no longer dropped before dispatch. This is the hard gate — without it tasks 2 and 3 are unreachable
- [ ] 4.2 Add the `scala` dispatch branch and an `_analyze_scala_file` method to `analysis/call_graph_analyzer.py`, plus the `lang-scala` node class alongside the existing `lang-kotlin` handling; verify a Scala file in a test repository produces components end to end
- [ ] 4.3 Map `.sc` to `"scala"` in `utils/patterns.py` `CODE_EXTENSIONS`, add `*.sc` to the code-file globs, and add Scala entries to `FUNCTION_DEFINITION_PATTERNS`; verify `.scala` and `.sc` both resolve to the `scala` language
- [ ] 4.4 Add `.scala` and `.sc` to the extension list in `ast_parser.py` `_file_to_module_path`; verify module paths for Scala files drop the extension
- [ ] 4.5 Add `.scala` and `.sc` to the source-extension set in `analyzers/artifact.py` so Scala files classify as code rather than falling through to artifact detection; verify a plain `.scala` file is not classified as an artifact
- [ ] 4.6 Add `.scala` and `.sc` to the fence-language map in `src/be/prompt_template.py`; verify a Scala snippet is tagged `scala`

## 5. sbt Build File Classification

- [ ] 5.1 Add `build.sbt` to `_MANIFEST_NAMES` and `.sbt` handling for `project/` files in `analyzers/artifact.py`; verify `build.sbt` classifies as a manifest and `project/plugins.sbt` as a build artifact
- [ ] 5.2 Confirm `.sbt` is absent from `CODE_EXTENSIONS` per design Decision 5; verify an `.sbt` file is never dispatched to the Scala analyzer

## 6. CLI and MCP Gates

- [ ] 6.1 Add `.scala` and `.sc` to `SUPPORTED_EXTENSIONS` in `cli/utils/repo_validator.py`; verify a repository containing only Scala files passes validation instead of being rejected as having no supported code
- [ ] 6.2 Add a `"Scala": [".scala", ".sc"]` entry to `detect_languages` in `cli/utils/validation.py`; verify Scala appears with a file count in detected language statistics
- [ ] 6.3 Add `.scala` and `.sc` to `source_extensions` in `mcp/tools/analysis.py`; verify a modified `.scala` file is reported as changed by incremental detection

## 7. Tests

- [ ] 7.1 Create `tests/test_scala_analyzer.py` modeled on `tests/test_ruby_analyzer.py`, with `pytest.importorskip("tree_sitter_scala")` and a Scala sample exercising a trait, a class with a companion object, inheritance, a mixin, an intra-type call, and a top-level definition; verify the suite runs
- [ ] 7.2 Add extraction assertions covering component types, owners, IDs, and the companion ` suffix; verify all pass
- [ ] 7.3 Add relationship assertions covering resolved inheritance, resolved mixin, resolved intra-file call, unresolved external target, and excluded built-in noise; verify all pass
- [ ] 7.4 Add a `DependencyParser` end-to-end test across two Scala files asserting cross-file inheritance resolves into `depends_on`; verify it passes
- [ ] 7.5 Add a Scala 3 sample covering `enum` and significant-indentation syntax; verify components are extracted
- [ ] 7.6 Add an artifact-classification test for `build.sbt` and `project/plugins.sbt`; verify both classify as build artifacts
- [ ] 7.7 Run the full existing test suite; verify no regressions in the Python, Kotlin, PHP, Ruby, or artifact analyzer tests

## 8. Documentation

- [ ] 8.1 Add Scala to the supported-languages list in `README.md`; verify it renders alongside the existing ten entries
- [ ] 8.2 Resolve the design's open questions on `given`, `extension`, `type` aliases, and `project/build.properties` against a real Scala 3 repository, and record the outcomes in `design.md`; verify each open question is either answered or explicitly carried forward
