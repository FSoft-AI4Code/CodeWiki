## Context

See proposal.md — Why. Two aspects of the current state shape this design.

**Scala is already half-wired.** `patterns.py` lists `*.scala` among code-file globs and
maps `".scala": "scala"` in `CODE_EXTENSIONS`. Scala files are therefore already
discovered and tagged with a language, then dropped at two later points:

```
  [CODE_EXTENSIONS]  .scala -> "scala"                   already wired
        |
  [call_graph_analyzer._find_code_files]                 passes through
        |
  [analysis_service._filter_supported_languages]         DROPPED (whitelist)
        |
  [call_graph_analyzer._analyze_file if/elif chain]       SILENT NO-OP (no branch,
        |                                                 else-warning commented out)
  [analyzers/scala.py]                                    does not exist
```

The whitelist is the hard gate. Until `"scala"` joins it, nothing else in this change
executes, and because the dispatch fallthrough logs nothing, the failure mode during
development is silence rather than an error.

**Leaf-node selection is typed, and its type set is narrow.** `leaf_selection.py`
defines `OOP_TYPES = {"class", "interface", "struct"}`. This drives two things: which
components survive `filter_leaf_nodes`, and the `n_oop` count feeding the issue-#75
heuristic that decides whether free functions carry a codebase's architecture. Component
types outside that set — `trait` (emitted by PHP), `object` (Kotlin), `enum` and
`record` (Java), `delegate` (C#), `type_alias` (C++) — are already excluded from leaf
selection today.

That exclusion is nearly invisible for existing languages because their codebases are
class-dominant. Scala is the first supported language where the excluded types *are* the
architecture: a typical Scala repository is mostly traits, objects, and case classes. A
faithful analyzer emitting `"trait"` and `"object"` would produce a repository whose
traits and objects are filtered out of documentation *and* whose `n_oop` count is near
zero, misfiring the heuristic into documenting loose methods instead of architectural
units.

## Goals / Non-Goals

**Goals:**

- Land Scala support without changing behavior for any already-supported language.
- Keep the whole change inside the 14-file footprint in proposal.md — Impact.
- Model Scala's distinctive constructs (traits, companion objects) so the dependency
  graph is correct rather than merely populated.

**Non-Goals:**

- Widening `OOP_TYPES`, adding duplicate-ID detection, or re-enabling the dispatch
  warning. See proposal.md — Non-Goals; each is pre-existing and gets its own change.
  Note that the `OOP_TYPES` gap is broader than PHP traits and Kotlin objects: C++
  `type_alias` components are dropped from leaf selection too, even though `cpp.py`
  extracts them deliberately on the grounds that aliases are real API surface. Whichever
  change widens the set should cover all of these together.
- Semantic analysis beyond syntactic extraction: no implicit resolution, no type
  inference, no macro expansion.
- Treating `.sbt` as an analyzable source language (see Decision 5).

## Decisions

### 1. Use the two-level `component_type` / `node_type` split

`component_type` carries the coarse type the pipeline gates on; `node_type` and
`display_name` carry the faithful Scala construct:

| Scala construct | `component_type` | `node_type` | `display_name` |
| --- | --- | --- | --- |
| `class` / `case class` | `class` | `class` | `class Foo` |
| `trait` | `interface` | `trait` | `trait Foo` |
| `object` / `package object` | `class` | `object` | `object Foo` |
| Scala 3 `enum` | `class` | `enum` | `enum Foo` |
| method | `method` | `method` | `method Foo.bar` |
| top-level definition | `function` | `function` | `function bar` |

*Why:* the coarse type keeps traits and objects leaf-eligible and counted toward
`n_oop`, so the issue-#75 heuristic reads a Scala repository accurately — while
`node_type` preserves what the construct actually is. No shared code changes, and no
information is discarded.

This is an established pattern, not a new one. `typescript.py:561-563` already splits
the two, deriving `component_type` from a coarse `type` and `node_type` from a finer
`include_functions` heuristic on existing repositories. That deserves a dedicated change
`component_type`. The `Node` model carries all three fields plus `get_display_name()`.

Critically, nothing recomputes `component_type` from `node_type`, so a deliberate split
cannot be clobbered downstream: `ast_parser._determine_component_type` — which has its
own competing whitelist including `enum`, `record`, `annotation`, and `delegate` — is
dead code, defined at `ast_parser.py:148` and never called.

*Alternative considered — remap onto the existing vocabulary and discard the construct*
(emit `component_type="interface"` for a trait with no faithful `node_type`). Rejected
once the split was found: it loses information for no benefit, since setting the extra
fields is free.

*Alternative considered — emit faithful `"trait"`/`"object"` as `component_type` and
widen `OOP_TYPES`.* Semantically cleanest, and it would fix the latent PHP, Kotlin, and
C++ gaps as a side effect. Rejected for this change: widening a globally shared set
changes documentation output for several shipped languages and can flip the
generated today reads from the coarse type, which the deferred `display_name` wiring
with a regression audit, not a rider on a new-language change.

*Alternative considered — emit faithful types as `component_type` and leave `OOP_TYPES`
alone.* Rejected: it would ship Scala support that produces near-empty documentation for
idiomatic Scala.

*Scope caveat — `node_type` and `display_name` are currently inert.* Neither flows into
generated documentation today: `get_display_name()` is never called, the MCP component
export at `mcp/tools/analysis.py:375` emits `component_type`, and `node_type` is read
only for the `artifact_file` check at `prompt_template.py:429` and a `"method"` test in
`call_graph_analyzer.py`. So setting these fields preserves the construct in the graph
and the exported artifacts at no cost, but it does **not** by itself make the docs say
"trait" rather than "interface". Surfacing faithful labels in documentation means wiring
`display_name` through the prompt path — a small change, deliberately not in this scope.
This design leaves the seam clean for it, and for the deferred `OOP_TYPES` work.

### 2. Suffix companion objects in the component identifier

Component IDs follow `relpath::Name` / `relpath::Class.method`. In Scala a companion
object is a *top-level peer* sharing its class's name, so `class Buffer` and
`object Buffer` both claim `buffer.scala::Buffer`. Because components are stored by
plain dict assignment with no collision check, the later-parsed declaration silently
displaces the earlier one — the class disappears from the graph while its methods remain
parented to the surviving object's node.

The object therefore takes a suffixed identifier: `buffer.scala::Buffer, mirroring
Scala's own JVM encoding of module classes. Members follow their owner
(`buffer.scala::Buffer.push`, `buffer.scala::Buffer$.apply`).

*Why:* both declarations survive as distinct components with their own source ranges and
documentation, and every edge stays attributable to the declaration that produced it.
The ` convention is one Scala developers already recognize from stack traces.

*Alternative considered — merge the companion's members into the class as static
members.* Closer to how developers think about companions, but it conflates two
disjoint source ranges into one component, so the generated documentation would show one
declaration's source text under a component holding the other's members.

*Note:* this is a local encoding choice, not a fix for the general silent-overwrite
defect, which is deferred.

### 3. Use the dedicated `tree-sitter-scala` package

*Why:* every one of the ten existing analyzers imports its own `tree_sitter_<lang>`
module and constructs a `Parser` from it. Following that pattern keeps the new analyzer
reviewable against its siblings.

*Alternative considered — `tree_sitter_language_pack`,* already a dependency and already
capable of Scala. Rejected as inconsistent with the established per-language pattern;
switching parser sourcing is a repo-wide decision, not a Scala one.

Version 0.26.2 introduces no `tree-sitter` core constraint in practice: its only core
pin is the optional `core` extra (`tree-sitter~=0.22`), which the pinned 0.23.2 already
satisfies. Wheels cover every CI platform. The grammar covers Scala 2 and 3, confirmed
against its published node types, which include `trait_definition`,
`object_definition`, `package_object`, `enum_definition`, `given_definition`, and
`extension_definition`.

### 4. Register the language at every gate, not just the analyzer

The whitelists in `analysis_service.py` (both the filter and the reported
supported-language list), the CLI's `SUPPORTED_EXTENSIONS` and language-detection map,
and the MCP incremental `source_extensions` set are all independent allowlists. Each
needs `.scala`/`.sc` or `"scala"` added. The Ruby precedent touched all of them; the
original requirements write-up named only some.

*Why it matters:* the analyzer being correct is not sufficient for the feature to work.
Missing `repo_validator.py` alone makes the CLI reject a pure-Scala repository outright.

### 5. Treat `.sbt` as a build artifact, never as analyzable source

`build.sbt` and `project/*.sbt` join the manifest and build classifications in
`artifact.py` alongside the existing `pom.xml`, `build.gradle`, and `build.gradle.kts`
entries. `.sbt` is *not* added to `CODE_EXTENSIONS`.

*Why:* `.sbt` files are syntactically Scala but semantically build configuration.
Documenting them as application source would misrepresent the architecture, while
leaving them unclassified means an sbt project's build story goes undocumented — a
visible gap now that artifact-aware generation has landed.

`project/*.scala` (sbt meta-build code) remains ordinary Scala source. Special-casing it
adds a path-shaped exception for modest benefit.

## Risks / Trade-offs

**The Kotlin analyzer is a structural template, not a copy-paste source.** Kotlin's
grammar uses `class_declaration` / `object_declaration` / `function_declaration`; Scala's
uses `class_definition` / `object_definition` / `function_definition`. Every `node.type`
comparison needs rewriting. → Treat Kotlin as the reference for *shape* (node
extraction, then relationship extraction, module-path derivation, ID construction) and
derive all node type names from the Scala grammar's own node types.

**Scala 3 significant-indentation syntax may parse poorly.** This is the only unknown
that could invalidate the extraction approach. → Spike first: parse representative
Scala 2 and Scala 3 files and assert the tree contains no `ERROR` nodes before building
extraction on top. Sequenced as the first task.

**Scala's expression-oriented style may produce noisy call graphs.** Heavy chaining,
higher-order functions, and for-comprehensions generate many call expressions against
standard-library targets. → Follow the precedent of the existing analyzers and filter
primitives and common built-ins, as Kotlin and PHP already do with their primitive sets;
assert the exclusion in tests.

**`.sc` is not exclusive to Scala** — SuperCollider and Scilab also use it. → Accepted:
the file is parsed with the Scala grammar and a non-Scala file simply yields no
components, matching how the other analyzers behave on unparseable input.

**Decision 1's coarse type is still a compromise.** A Scala trait carries
`component_type="interface"`, so any consumer reading only the coarse type sees an
interface. → Largely mitigated by the split: `node_type="trait"` and
`display_name="trait Foo"` keep the construct in the graph, so no information is lost
and a future consumer can render it faithfully. The residual issue is that documentation
would address.
`ruby.py:608-611` sets `node_type` and a `"<type> <name>"` `display_name` alongside
`subtype` (a TS type alias is `component_type="type"`, `node_type="type_alias"`).

## Open Questions

These can be answered during implementation without changing the specs, the approach,
or the task breakdown:

- **Scala 3 `given` definitions.** Typeclass instances are architecturally meaningful,
  but whether each is a *documentable component* is unclear. Leaning toward extracting
  named givens as `"class"` and skipping anonymous ones. Requires a real Scala 3
  codebase to judge signal versus noise.
- **Scala 3 `extension` blocks.** The methods inside are plausibly `"method"` components,
  but their owner is the extended type, which may live outside the repository.
- **`type` aliases.** The existing `"type_alias"` component type sits outside
  `OOP_TYPES` and would be dropped; likely skip rather than mismap.
- **`project/build.properties`.** Whether the sbt version pin is worth classifying as a
  build artifact, or is too granular to document.
