## Why

CodeWiki supports ten languages but not Scala, and users have asked for it to unify
JVM workflows (issue #105). The groundwork is already partly in place — `.scala` is
registered in `CODE_EXTENSIONS` and the tree-sitter Scala grammar exists — but Scala
files are recognized, tagged `language: "scala"`, and then silently discarded before
any analyzer runs. Today a Scala repository produces no documentation and no error.

## What Changes

- Add a tree-sitter Scala analyzer (`analyzers/scala.py`) that extracts classes,
  traits, objects, enums, and methods and emits call/inheritance relationships.
- Register `.scala` and `.sc` as analyzable source across discovery, dispatch,
  validation, and incremental-change detection.
- Admit `"scala"` to the two language whitelists in `analysis_service.py`. This is the
  hard gate: without it every other change in this proposal is unreachable code.
- Classify `build.sbt` and `project/*.sbt` as build manifests so sbt-based projects get
  their build story documented alongside the existing Maven/Gradle handling.
- Add `tree-sitter-scala` to `pyproject.toml` and `requirements.txt`.
- Add `tests/test_scala_analyzer.py` covering extraction, relationships, and an
  end-to-end `DependencyParser` pass.
- Update the supported-languages list in `README.md`.

No breaking changes. Existing languages are untouched: Scala constructs are mapped onto
the current `component_type` vocabulary rather than widening the shared `OOP_TYPES` set
(see design.md).

## Capabilities

### New Capabilities
- `scala-language-support`: Discovery, parsing, component extraction, and dependency
  analysis for Scala source files, plus classification of sbt build files.

### Modified Capabilities

None. `openspec/specs/` is currently empty, so no existing capability's requirements
change.

## Impact

**Dependencies.** Adds `tree-sitter-scala` (0.26.2). No `tree-sitter` core bump: its
only core pin is the optional `core` extra (`tree-sitter~=0.22`), which the pinned
0.23.2 already satisfies. abi3-cp39 wheels are published for macOS x86_64/arm64,
manylinux x86_64/aarch64, musllinux, and Windows amd64/arm64, so no source builds in
CI. The grammar covers both Scala 2 and Scala 3.

**Affected code** — 14 files, matching the footprint of the Ruby precedent (PR #97,
commit `05c7576`):

| File | Change |
| --- | --- |
| `codewiki/src/be/dependency_analyzer/analyzers/scala.py` | new analyzer |
| `codewiki/src/be/dependency_analyzer/analysis/analysis_service.py` | `"scala"` in both language whitelists |
| `codewiki/src/be/dependency_analyzer/analysis/call_graph_analyzer.py` | dispatch branch, `lang-scala` node class |
| `codewiki/src/be/dependency_analyzer/utils/patterns.py` | `.sc` mapping, Scala function patterns |
| `codewiki/src/be/dependency_analyzer/ast_parser.py` | strip `.scala`/`.sc` in module paths |
| `codewiki/src/be/dependency_analyzer/analyzers/artifact.py` | source exts, sbt manifests |
| `codewiki/src/be/prompt_template.py` | `.scala`/`.sc` fence-language mapping |
| `codewiki/cli/utils/repo_validator.py` | `SUPPORTED_EXTENSIONS` |
| `codewiki/cli/utils/validation.py` | `detect_languages` map |
| `codewiki/mcp/tools/analysis.py` | `source_extensions` for incremental detection |
| `pyproject.toml`, `requirements.txt` | dependency |
| `tests/test_scala_analyzer.py` | new tests |
| `README.md` | supported-languages list |

The last six are absent from the original requirements write-up but were all touched
when Ruby was added. Three of them are user-visible: without `repo_validator.py` the
CLI rejects a pure-Scala repository as containing no supported code, without
`validation.py` Scala is missing from detected-language statistics, and without
`mcp/tools/analysis.py` edits to `.scala` files never trigger incremental
regeneration.

## Non-Goals

Three adjacent defects were found while scoping this change. All are pre-existing, none
are Scala-specific, and each is deferred to its own change:

- **`OOP_TYPES` excludes `trait`, `object`, `enum`, and `record`.** PHP already emits
  `"trait"` and Kotlin `"object"`, so both are already dropped from leaf-node selection.
  Widening the set is the semantically correct fix but changes documentation output for
  three shipped languages and needs a regression audit.
- **Duplicate component IDs overwrite silently.** `ast_parser.py:109` and the ten
  `self.functions[func_id] = func` sites in `call_graph_analyzer.py` are plain dict
  assignments with no collision detection.
- **Unsupported-language dispatch is silent.** The `else: logger.warning(...)` at
  `call_graph_analyzer.py:257` is commented out, which is why Scala files currently
  fail without a diagnostic.
