# Dependency Analysis Service

## Purpose

The **Dependency Analysis Service** is the orchestration layer that turns a raw repository (local folder or a
GitHub URL) into a structured, language-aware **call graph**: a set of `Node` objects (functions, methods,
classes, ...) and `CallRelationship` edges between them. It is the entry point used by higher-level components
—most notably `DependencyParser` in [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md) and the
[Backend_LLM_&_Documentation_Services](Backend_LLM_%26_Documentation_Services.md) documentation pipeline—to
obtain the data that ultimately feeds LLM-driven documentation generation.

The module has three responsibilities, each owned by one component:

| Component | File | Responsibility |
|---|---|---|
| `AnalysisService` | `analysis/analysis_service.py` | Top-level orchestrator: clone/locate a repo, drive structure analysis, drive call-graph analysis, assemble the final `AnalysisResult`, and clean up temporary state. |
| `RepoAnalyzer` / `GitIgnoreFilter` | `analysis/repo_analyzer.py` | Walk the file system, build a filtered file tree (include/exclude glob patterns + Git ignore semantics), and compute summary statistics. |
| `CallGraphAnalyzer` | `analysis/call_graph_analyzer.py` | Dispatch each discovered source file to the appropriate per-language analyzer (see [Language_Analyzers](Language_Analyzers.md)), collect functions/relationships, resolve cross-file/cross-language calls, and produce visualization-ready data. |

## Architecture Overview

```mermaid
flowchart TB
    subgraph Consumers
        DP["DependencyParser<br/>(Dependency_Analyzer_Core)"]
        DG["DocumentationGenerator<br/>(Backend_LLM_&_Documentation_Services)"]
    end

    subgraph DAS["Dependency Analysis Service"]
        AS["AnalysisService<br/>(orchestrator)"]
        RA["RepoAnalyzer / GitIgnoreFilter<br/>(file tree + filtering)"]
        CGA["CallGraphAnalyzer<br/>(call graph construction)"]
    end

    subgraph LA["Language_Analyzers"]
        PY["PythonASTAnalyzer"]
        JS["TreeSitterJSAnalyzer / TS"]
        JVM["Java / Kotlin / C# analyzers"]
        CFAM["C / C++ analyzers"]
        PHP["PHP analyzer"]
    end

    Cloning["cloning.py<br/>clone_repository / parse_github_url"]

    DP --> AS
    DG -.-> DP
    AS --> Cloning
    AS --> RA
    AS --> CGA
    CGA --> PY
    CGA --> JS
    CGA --> JVM
    CGA --> CFAM
    CGA --> PHP

    AS --> Result["AnalysisResult<br/>(Dependency_Analyzer_Core models)"]
```

## Data Flow: Full Repository Analysis

`AnalysisService.analyze_repository_full` is the richest entry point. It performs cloning, structure discovery,
call-graph construction, and packages everything into an `AnalysisResult` (defined in
[Dependency_Analyzer_Core](Dependency_Analyzer_Core.md)).

```mermaid
sequenceDiagram
    participant Caller
    participant AS as AnalysisService
    participant Clone as cloning.py
    participant RA as RepoAnalyzer
    participant CGA as CallGraphAnalyzer
    participant LangA as Language Analyzer

    Caller->>AS: analyze_repository_full(github_url)
    AS->>Clone: clone_repository(github_url)
    Clone-->>AS: temp_dir
    AS->>RA: analyze_repository_structure(temp_dir)
    RA-->>AS: file_tree + summary
    AS->>CGA: extract_code_files(file_tree)
    CGA-->>AS: code_files (filtered by extension)
    AS->>CGA: analyze_code_files(code_files, temp_dir)
    loop for each source file
        CGA->>LangA: analyze_<language>_file(path, content)
        LangA-->>CGA: (functions, relationships)
    end
    CGA->>CGA: resolve_call_relationships()
    CGA->>CGA: deduplicate_relationships()
    CGA-->>AS: functions, relationships, visualization
    AS->>AS: read README, build Repository + AnalysisResult
    AS->>Clone: cleanup_repository(temp_dir)
    AS-->>Caller: AnalysisResult
```

## Component Details

### `AnalysisService`

Central façade exposing three analysis modes:

- **`analyze_repository_full(github_url, ...)`** — clones a remote repository, runs structure + call-graph
  analysis, reads the README, and returns a fully populated `AnalysisResult` (functions, relationships, file
  tree, summary, Cytoscape visualization data). Cleans up the temporary clone directory afterward (also
  tracked in `_temp_directories` and cleaned via `cleanup_all()` / `__del__` as a safety net).
- **`analyze_repository_structure_only(github_url, ...)`** — lightweight variant that skips call-graph
  generation, useful for quick previews of repository layout.
- **`analyze_local_repository(repo_path, ...)`** — analyzes a folder already present on disk (no cloning),
  used for local/offline workflows; returns a simplified dict (`nodes`, `relationships`, `summary`) rather
  than a full `AnalysisResult`.

Internally it composes a `RepoAnalyzer` (structure) and a `CallGraphAnalyzer` (call graph), and delegates
repository acquisition to `cloning.py` helpers (`clone_repository`, `cleanup_repository`, `parse_github_url`).
It also filters files down to the set of currently supported languages (Python, JavaScript, TypeScript, Java,
C#, C, C++, PHP, Kotlin) before invoking the call-graph analyzer.

Two module-level functions, `analyze_repository` and `analyze_repository_structure_only`, are kept as
backward-compatible wrappers around a freshly constructed `AnalysisService`.

```mermaid
classDiagram
    class AnalysisService {
        +call_graph_analyzer: CallGraphAnalyzer
        +analyze_local_repository(repo_path, max_files, languages, use_gitignore)
        +analyze_repository_full(github_url, include_patterns, exclude_patterns, use_gitignore) AnalysisResult
        +analyze_repository_structure_only(github_url, ...) Dict
        -_clone_repository(github_url) str
        -_analyze_structure(repo_dir, ...) Dict
        -_analyze_call_graph(file_tree, repo_dir) Dict
        -_filter_supported_languages(code_files) List
        -_cleanup_repository(temp_dir)
        +cleanup_all()
    }
    class RepoAnalyzer
    class CallGraphAnalyzer
    AnalysisService --> RepoAnalyzer : uses
    AnalysisService --> CallGraphAnalyzer : uses
```

### `RepoAnalyzer` and `GitIgnoreFilter`

`RepoAnalyzer.analyze_repository_structure(repo_dir)` recursively walks a directory into a nested `file_tree`
dict (`type`, `name`, `path`, `extension`/`children`), applying:

- **Include patterns** (`fnmatch`-style globs) — if provided, they *replace* the defaults
  (`DEFAULT_INCLUDE_PATTERNS`); otherwise all files are eligible for inclusion.
- **Exclude patterns** — merged with `DEFAULT_IGNORE_PATTERNS`; matched against full relative path, filename,
  and path segments.
- **Git ignore semantics** via `GitIgnoreFilter`, which prefers shelling out to `git ls-files --others
  --ignored --exclude-standard` (so nested `.gitignore` files, global excludes, and tracked-file rules behave
  exactly like Git) and falls back to a `pathspec`-based scan of every `.gitignore` file in the tree when Git
  is unavailable or the folder isn't a Git worktree.

Symlinks and paths that resolve outside the repository root are rejected defensively while building the tree.
The result also includes a summary (`total_files`, `total_size_kb`).

```mermaid
flowchart LR
    Start["analyze_repository_structure(repo_dir)"] --> Filter["Build GitIgnoreFilter\n(git ls-files or pathspec fallback)"]
    Filter --> Walk["Recursively walk directory"]
    Walk --> Exclude{"_should_exclude_path?\n(exclude patterns / gitignore / symlink)"}
    Exclude -- yes --> Skip["Drop node"]
    Exclude -- no --> Include{"file? _should_include_file?"}
    Include -- file & matches --> Leaf["Add file node"]
    Include -- directory --> Recurse["Recurse into children"]
    Leaf --> Tree["file_tree"]
    Recurse --> Tree
    Tree --> Summary["total_files / total_size_kb"]
```

### `CallGraphAnalyzer`

The multi-language orchestrator. Given a list of code files (already filtered to supported extensions via
`extract_code_files`), it:

1. **Routes ambiguous `.h` headers** (`_route_contextual_headers`) to either C or C++ based on content signals
   (namespaces, classes, templates, `::`, or known C++ standard headers) and repository composition.
2. **Collects Python module names** up front (`_collect_python_modules`) to support accurate project-vs-external
   import classification later.
3. **Dispatches each file** to the language-specific analyzer function based on `file_info["language"]`,
   delegating to the implementations documented in [Language_Analyzers](Language_Analyzers.md):
   - `python` → `analyzers/python.py::analyze_python_file`
   - `javascript` / `typescript` → tree-sitter analyzers in `analyzers/javascript.py` / `typescript.py`
   - `java`, `kotlin`, `csharp`, `c`, `cpp`, `php` → respective tree-sitter analyzers
   Each file is analyzed under a 30-second timeout guard (`timeout` context manager, POSIX `SIGALRM`-based,
   no-op on Windows) so a pathological file cannot stall the whole run.
4. **Resolves call relationships** (`_resolve_call_relationships`): builds global and per-language exact/
   simple-name lookup indexes (`_build_resolution_indexes`) from `id`, `component_id`, `qualified_name`, and
   `name`, then attempts to match each unresolved `CallRelationship.callee` — preferring matches within the
   caller's own language, falling back to language-qualified/simple-name matching, and finally dropping
   relationships that are provably external (standard library calls, C/C++ macros, or third-party
   package/module calls) via `is_external_symbol`/`is_macro_name` and dotted-package/module heuristics.
5. **Deduplicates** relationships on `(caller, callee)` pairs.
6. **Generates visualization data**: a Cytoscape.js-compatible `{elements: [...]}` structure with per-language
   CSS classes (`lang-python`, `lang-cpp`, ...) and node/edge classification, plus a resolution summary
   (`total_nodes`, `total_edges`, `unresolved_calls`).

It also exposes `generate_llm_format()` (a compact, LLM-friendly functions/relationships view) and
`_select_most_connected_nodes(target_count)` (degree-centrality-based pruning for very large graphs).

```mermaid
flowchart TB
    Files["code_files (filtered by language)"] --> Route["_route_contextual_headers\n(.h -> c/cpp)"]
    Route --> PyMods["_collect_python_modules"]
    PyMods --> Loop["For each file: _analyze_code_file\n(30s timeout guard)"]
    Loop -->|python| PyA["analyzers.python"]
    Loop -->|js/ts| JsA["analyzers.javascript / typescript"]
    Loop -->|java/kotlin/csharp| JvmA["analyzers.java / kotlin / csharp"]
    Loop -->|c/cpp| CA["analyzers.c / cpp"]
    Loop -->|php| PhpA["analyzers.php"]
    PyA & JsA & JvmA & CA & PhpA --> Collect["functions{} + call_relationships[]"]
    Collect --> Resolve["_resolve_call_relationships\n(exact/simple name indexes,\nlanguage-aware, external filtering)"]
    Resolve --> Dedup["_deduplicate_relationships"]
    Dedup --> Viz["_generate_visualization_data\n(Cytoscape elements)"]
    Viz --> Out["{call_graph, functions, relationships, visualization}"]
```

## Key Data Structures

The service produces/consumes model types defined in
[Dependency_Analyzer_Core](Dependency_Analyzer_Core.md#models):

- **`Node`** — a single code component (function, method, class, ...) with identity (`id`, `component_id`,
  `qualified_name`), location (`file_path`, `relative_path`, `start_line`/`end_line`), documentation
  (`docstring`), and its outgoing `depends_on` set.
- **`CallRelationship`** — a directed edge `caller -> callee`, with `call_line` and an `is_resolved` flag set
  once `CallGraphAnalyzer` matches the callee to a concrete `Node`.
- **`Repository`** — metadata about the analyzed repo (`url`, `name`, `clone_path`, `analysis_id`).
- **`AnalysisResult`** — the full output bundle: `repository`, `functions` (list of `Node`), `relationships`
  (list of `CallRelationship`), `file_tree`, `summary`, `visualization`, and optional `readme_content`.

## Position in the Overall System

```mermaid
flowchart LR
    User["End user / API request"] --> DG["DocumentationGenerator<br/>(Backend_LLM_&_Documentation_Services)"]
    DG --> DP["DependencyParser + DependencyGraphBuilder<br/>(Dependency_Analyzer_Core)"]
    DP --> DAS["Dependency Analysis Service<br/>(this module)"]
    DAS --> LA["Language_Analyzers"]
    DAS -->|Node / CallRelationship models| DP
    DP -->|leaf-node components + graph| DG
    DG -->|documentation content| FE["Frontend_Web_App / CLI"]
```

- **[Dependency_Analyzer_Core](Dependency_Analyzer_Core.md)** consumes this service directly:
  `DependencyParser.parse_repository()` instantiates an `AnalysisService`, calls its private
  `_analyze_structure` / `_analyze_call_graph` helpers, and converts the resulting functions/relationships
  dicts into `Node` objects with `depends_on` edges, which `DependencyGraphBuilder` then turns into a
  traversable dependency graph and a set of "leaf" components.
- **[Language_Analyzers](Language_Analyzers.md)** supplies every per-language `analyze_*_file` function that
  `CallGraphAnalyzer` dispatches to; this module is language-agnostic and treats each analyzer as a pluggable
  strategy returning `(functions, relationships[, extra])`.
- **[Backend_LLM_&_Documentation_Services](Backend_LLM_%26_Documentation_Services.md)** sits above
  `Dependency_Analyzer_Core` and uses the resulting component graph as the substrate for LLM-driven
  documentation generation (see `DocumentationGenerator`).
- Repository acquisition (`git clone`, URL parsing) is handled by `analysis/cloning.py`, a small internal
  helper module used exclusively by `AnalysisService`.

## Design Notes

- **Fail-safe cleanup**: every cloning path is wrapped in try/except blocks that clean up the temporary
  directory on failure, and `AnalysisService` also tracks all created temp dirs for a final `cleanup_all()` /
  `__del__` safety net.
- **Security-conscious file reads**: file content and README lookups go through `safe_open_text` /
  `assert_safe_path` to guard against path traversal outside the repository root.
- **Language extensibility**: adding a new language mainly requires (a) a new `analyze_<lang>_file` function
  in [Language_Analyzers](Language_Analyzers.md), (b) a dispatch branch in
  `CallGraphAnalyzer._analyze_code_file`, and (c) adding the language to
  `AnalysisService._filter_supported_languages` / `_get_supported_languages`.
- **Resolution favors precision over recall**: relationships that cannot be confidently resolved to a known
  project `Node` — and are provably external (stdlib, macros, third-party imports) — are dropped rather than
  kept as noisy unresolved edges, keeping the resulting call graph focused on in-repository architecture.
