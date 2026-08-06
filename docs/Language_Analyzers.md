# Language Analyzers

## Purpose

The **Language_Analyzers** module is the collection of language-specific static analysis "front ends" that power CodeWiki's [Dependency_Analysis_Service](Dependency_Analysis_Service.md). For each supported programming language, an analyzer parses a single source file — using either [tree-sitter](https://tree-sitter.github.io/) grammars or, for Python, the native `ast` module — and extracts:

1. **`Node` records** — one per top-level declaration (class, struct, interface, function, method, trait, enum, global variable, type alias, …), representing a documentable "component".
2. **`CallRelationship` records** — directed edges describing how those components reference each other (calls, inheritance, interface implementation, instantiation, field/property types, imports, …).

None of these analyzers attempt full semantic/compiler-grade resolution. Instead, each performs the best-effort, file-local resolution possible from syntax alone, marking relationships `is_resolved=True` when the target is confidently known within the same file, and `is_resolved=False` (a best-effort qualified/dotted name) otherwise. The heavy lifting of matching unresolved edges to real components anywhere in the repository — and filtering out edges to genuinely external/third-party/stdlib symbols — is centralized in `CallGraphAnalyzer`, part of [Dependency_Analysis_Service](Dependency_Analysis_Service.md). The resulting resolved call graph is then transformed into the project-wide dependency graph by [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md), which ultimately feeds [Backend_LLM_&_Documentation_Services](Backend_LLM_&_Documentation_Services.md) for documentation generation.

Currently supported languages/analyzers:

| Language(s) | Analyzer(s) | Parsing technology |
|---|---|---|
| C, C++, C#, Java, Kotlin | `TreeSitterCAnalyzer`, `TreeSitterCppAnalyzer`, `TreeSitterCSharpAnalyzer`, `TreeSitterJavaAnalyzer`, `TreeSitterKotlinAnalyzer` | tree-sitter |
| JavaScript, TypeScript | `TreeSitterJSAnalyzer`, `TreeSitterTSAnalyzer` | tree-sitter |
| PHP | `TreeSitterPHPAnalyzer` (+ `NamespaceResolver`) | tree-sitter |
| Python | `PythonASTAnalyzer` | native `ast` |

## Architecture

### Position in the system

```mermaid
graph TD
    RA["RepoAnalyzer<br/>(file discovery)"] --> AS["AnalysisService"]
    AS --> CGA["CallGraphAnalyzer"]
    CGA -->|routes by file extension| CFAM["C-Family Tree-sitter Analyzers<br/>(C, C++, C#, Java, Kotlin)"]
    CGA -->|routes by file extension| JSTS["JavaScript & TypeScript Analyzers"]
    CGA -->|routes by file extension| PHP["PHP Analyzer"]
    CGA -->|routes by file extension| PY["Python Analyzer"]
    CFAM -->|Node + CallRelationship lists| CGA
    JSTS -->|Node + CallRelationship lists| CGA
    PHP -->|Node + CallRelationship lists| CGA
    PY -->|Node + CallRelationship lists + external roots| CGA
    CGA -->|cross-file resolution + external filtering| DP["DependencyParser"]
    DP --> DGB["DependencyGraphBuilder"]
    DGB --> Docs["DocumentationGenerator"]

    click AS "Dependency_Analysis_Service.md"
    click CGA "Dependency_Analysis_Service.md"
    click DP "Dependency_Analyzer_Core.md"
    click DGB "Dependency_Analyzer_Core.md"
    click Docs "Backend_LLM_&_Documentation_Services.md"
```

### Shared data contract

Every analyzer in this module builds instances of the same two Pydantic models, owned by [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md):

```mermaid
classDiagram
    class Node {
        +str id
        +str name
        +str component_type
        +str file_path
        +str relative_path
        +str source_code
        +int start_line
        +int end_line
        +str docstring
        +str class_name
        +str display_name
        +str component_id
        +str qualified_name
        +Set~str~ depends_on
    }
    class CallRelationship {
        +str caller
        +str callee
        +int call_line
        +bool is_resolved
    }
    Node "1" --> "*" CallRelationship : caller/callee reference Node.id
```

* **`Node.id` / `component_id`**: `"{relative_path_from_repo_root}::{qualified_name}"`, e.g. `src/Foo.cpp::Logger.write` — the canonical identifier used across CodeWiki.
* **`CallRelationship.is_resolved`**: `True` when the callee is a confirmed same-file `Node.id`; `False` when only a name (simple, qualified, or dotted) is known, deferring final resolution to `CallGraphAnalyzer`'s repository-wide indexes and external-symbol classifier.

### Common analyzer pipeline

Despite covering very different languages/grammars, all analyzers follow the same three-phase pattern:

```mermaid
sequenceDiagram
    participant CGA as CallGraphAnalyzer
    participant AN as Language Analyzer
    participant Parser as Grammar (tree-sitter / ast)
    participant Model as Node / CallRelationship

    CGA->>AN: analyze_<lang>_file(file_path, content, repo_path)
    AN->>Parser: parse(content)
    Parser-->>AN: syntax tree
    AN->>AN: extract declarations (Node candidates)
    AN->>Model: construct Node per declaration
    AN->>AN: extract references (calls, inheritance, imports, types)
    AN->>Model: construct CallRelationship per reference
    AN-->>CGA: (nodes, call_relationships)
```

### Output flow

```mermaid
flowchart LR
    subgraph "Language_Analyzers"
        CF["C-Family Analyzers"]
        JT["JS/TS Analyzers"]
        PH["PHP Analyzer"]
        PY["Python Analyzer"]
    end
    CF & JT & PH & PY --> Funcs["Nodes"]
    CF & JT & PH & PY --> Rels["CallRelationships"]
    Funcs --> Resolve["CallGraphAnalyzer._resolve_call_relationships"]
    Rels --> Resolve
    Resolve --> Filter["_is_external_callee<br/>(drops stdlib/third-party edges)"]
    Filter --> Graph["Complete call graph"]
    Graph --> DP["DependencyParser"]
    DP --> DGB["DependencyGraphBuilder"]
    DGB --> Gen["DocumentationGenerator"]
```

## Sub-modules & Core Components

| Sub-module | Languages | Key Components | Documentation |
|---|---|---|---|
| **C-Family Tree-sitter Analyzers** | C, C++, C#, Java, Kotlin | `TreeSitterCAnalyzer`, `TreeSitterCppAnalyzer`, `TreeSitterCSharpAnalyzer`, `TreeSitterJavaAnalyzer`, `TreeSitterKotlinAnalyzer` | [C-Family_Tree-sitter_Analyzers.md](C-Family_Tree-sitter_Analyzers.md) |
| **JavaScript & TypeScript Analyzers** | JavaScript, TypeScript | `TreeSitterJSAnalyzer`, `TreeSitterTSAnalyzer` | [JavaScript_TypeScript_Analyzers.md](JavaScript_TypeScript_Analyzers.md) |
| **PHP Analyzer** | PHP | `TreeSitterPHPAnalyzer`, `NamespaceResolver` | [PHP_Analyzer.md](PHP_Analyzer.md) |
| **Python Analyzer** | Python | `PythonASTAnalyzer` | [Python_Analyzer.md](Python_Analyzer.md) |

## Related Modules

* [Dependency_Analysis_Service](Dependency_Analysis_Service.md) — owns `CallGraphAnalyzer`, which discovers files, dispatches them to the appropriate analyzer in this module, and performs cross-file symbol resolution and external-symbol filtering.
* [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md) — defines the shared `Node`/`CallRelationship` models and the `DependencyParser`/`DependencyGraphBuilder` that turn resolved call graphs into the final dependency graph.
* [Backend_LLM_&_Documentation_Services](Backend_LLM_&_Documentation_Services.md) — consumes the dependency graph to generate human-readable documentation.