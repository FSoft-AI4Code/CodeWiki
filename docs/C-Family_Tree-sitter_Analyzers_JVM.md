# C-Family Tree-sitter Analyzers: JVM (Java & Kotlin)

## Introduction

The **C-Family Tree-sitter Analyzers (JVM)** module provides static-analysis capabilities for Java and Kotlin source files within the CodeWiki dependency-analysis pipeline. It is implemented as two sibling analyzers — `TreeSitterJavaAnalyzer` and `TreeSitterKotlinAnalyzer` — that each parse a single source file with [tree-sitter](https://tree-sitter.github.io/tree-sitter/) grammars (`tree_sitter_java` / `tree_sitter_kotlin`), extract structural code components (classes, interfaces, enums, records, annotations, objects, methods, functions), and record inheritance/usage/call relationships between them.

Compared to the [C-Family_Tree-sitter_Analyzers_C_Cpp](C-Family_Tree-sitter_Analyzers_C_Cpp.md) sibling, the JVM analyzers are distinguished by **package/import-aware name resolution**: Java code is organized around `package` declarations and explicit `import` statements (including wildcard imports and static imports), which the Java analyzer parses and uses to qualify every type reference it encounters. This lets it resolve simple names like `Logger` to their fully-qualified form (`com.example.util.Logger`) and to reliably distinguish project types from JDK/runtime types using the shared [`is_external_symbol`](Dependency_Analyzer_Core.md) classifier. The Kotlin analyzer is comparatively lighter-weight (no import-table construction) and relies on a curated built-in/primitive-type set plus local variable/parameter scanning to avoid false-positive edges to standard-library types.

These analyzers are two of several language-specific plugins invoked by the [Dependency_Analysis_Service](Dependency_Analysis_Service.md)'s `CallGraphAnalyzer`, which orchestrates multi-language repository analysis, cross-file/cross-language relationship resolution, and external-symbol filtering. The analyzers documented here perform only **single-file, language-specific extraction** — all cross-file resolution and unresolved-call filtering happens centrally afterward in `CallGraphAnalyzer`.

This module is a sibling to the other Tree-sitter-based language analyzer families under [Language_Analyzers](C-Family_Tree-sitter_Analyzers.md):
- [C-Family_Tree-sitter_Analyzers_C_Cpp](C-Family_Tree-sitter_Analyzers_C_Cpp.md) (C, C++)
- [C-Family_Tree-sitter_Analyzers_CSharp](C-Family_Tree-sitter_Analyzers_CSharp.md) (C#)
- [JavaScript_TypeScript_Analyzers](JavaScript_TypeScript_Analyzers.md)
- [PHP_Analyzer](PHP_Analyzer.md)
- [Python_Analyzer](Python_Analyzer.md)

All of these plug into the same [Dependency_Analysis_Service](Dependency_Analysis_Service.md) and produce the same `Node` / `CallRelationship` data model defined in [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md).

---

## Module Position in the System

```mermaid
graph TB
    subgraph "Dependency_Analysis_Service"
        AS[AnalysisService]
        RA[RepoAnalyzer]
        CGA[CallGraphAnalyzer]
    end

    subgraph "Language_Analyzers"
        subgraph "C-Family_Tree-sitter_Analyzers_JVM (this module)"
            JA[TreeSitterJavaAnalyzer<br/>java.py]
            KA[TreeSitterKotlinAnalyzer<br/>kotlin.py]
        end
        CCPP[C-Family_Tree-sitter_Analyzers_C_Cpp]
        CS[C-Family_Tree-sitter_Analyzers_CSharp]
        JSTS[JavaScript_TypeScript_Analyzers]
        PHP[PHP_Analyzer]
        PY[Python_Analyzer]
    end

    subgraph "Dependency_Analyzer_Core"
        Node[Node model]
        CR[CallRelationship model]
        DP[DependencyParser]
        DGB[DependencyGraphBuilder]
    end

    subgraph "Shared Utilities"
        ES[is_external_symbol<br/>external_symbols.py]
    end

    RA --> AS
    AS --> CGA
    CGA -->|routes .java files| JA
    CGA -->|routes .kt / .kts files| KA
    CGA --> CCPP
    CGA --> CS
    CGA --> JSTS
    CGA --> PHP
    CGA --> PY

    JA -->|produces| Node
    JA -->|produces| CR
    JA -->|consults| ES
    KA -->|produces| Node
    KA -->|produces| CR

    CGA -->|resolves & filters| CR
    DP -->|consumes AnalysisService output| AS
    DP --> Node
    DGB --> Node
```

The `CallGraphAnalyzer` (see [Dependency_Analysis_Service](Dependency_Analysis_Service.md)) dispatches per file:
- `.java` files → `TreeSitterJavaAnalyzer` (via `_analyze_java_file`)
- `.kt` / `.kts` files → `TreeSitterKotlinAnalyzer` (via `_analyze_kotlin_file`)

Both analyzers emit `Node` and `CallRelationship` lists that flow, unmodified in shape, into `CallGraphAnalyzer`'s repository-wide resolution indexes and external-symbol filter, and from there into [`DependencyParser`](Dependency_Analyzer_Core.md) and [`DependencyGraphBuilder`](Dependency_Analyzer_Core.md).

---

## Component Overview

```mermaid
classDiagram
    class TreeSitterJavaAnalyzer {
        +file_path: Path
        +content: str
        +repo_path: str
        +nodes: List~Node~
        +call_relationships: List~CallRelationship~
        +package_name: str
        +import_map: dict~str,str~
        +wildcard_imports: list~str~
        -_analyze()
        -_extract_nodes(node, top_level_nodes, lines)
        -_extract_relationships(node, top_level_nodes)
        -_extract_package_name() str
        -_extract_imports() tuple
        -_resolve_java_type(type_name, context_node, top_level_nodes) str
        -_resolve_java_member(member_name, context_node, top_level_nodes, target_type) str
        -_is_primitive_type(type_name) bool
        -_skip_type(type_name, context_node) bool
        -_find_variable_type(node, variable_name, top_level_nodes) str
        -_find_containing_class(node, top_level_nodes) str
        -_find_containing_method(node) str
        -_get_component_id(name, parent_class) str
    }

    class TreeSitterKotlinAnalyzer {
        +file_path: Path
        +content: str
        +repo_path: str
        +nodes: List~Node~
        +call_relationships: List~CallRelationship~
        -_analyze()
        -_extract_nodes(node, top_level_nodes, lines)
        -_extract_relationships(node, top_level_nodes)
        -_get_class_modifiers(class_node) set
        -_is_primitive_type(type_name) bool
        -_find_variable_type(node, variable_name, top_level_nodes) str
        -_find_containing_class(node, top_level_nodes) str
        -_find_containing_method(node) str
        -_get_component_id(name, parent_class) str
    }

    class Node {
        <<pydantic model>>
        id: str
        name: str
        component_type: str
        file_path: str
        relative_path: str
        source_code: str
        start_line: int
        end_line: int
        node_type: str
        class_name: str
        display_name: str
        component_id: str
        language: str
        qualified_name: str
    }

    class CallRelationship {
        <<pydantic model>>
        caller: str
        callee: str
        call_line: int
        is_resolved: bool
    }

    class is_external_symbol {
        <<function>>
        +is_external_symbol(language, symbol) bool
    }

    TreeSitterJavaAnalyzer --> Node : creates
    TreeSitterJavaAnalyzer --> CallRelationship : creates
    TreeSitterJavaAnalyzer --> is_external_symbol : uses for JDK filtering
    TreeSitterKotlinAnalyzer --> Node : creates
    TreeSitterKotlinAnalyzer --> CallRelationship : creates
```

See [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md) for the full `Node` and `CallRelationship` model definitions used throughout all language analyzers.

---

## `TreeSitterJavaAnalyzer` (java.py)

### Purpose

Parses a single `.java` file using the `tree_sitter_java` grammar and extracts:
- **Classes** (`class_declaration`), further distinguished as `class` vs `abstract class` via modifier inspection
- **Interfaces** (`interface_declaration`)
- **Enums** (`enum_declaration`)
- **Records** (`record_declaration`)
- **Annotations** (`annotation_type_declaration`)
- **Methods** (`method_declaration`), qualified with their containing type as `ClassName.methodName`

It records relationships for:
1. **Inheritance** — `class X extends Y` (`superclass` clause)
2. **Interface implementation** — `class/enum/record X implements Y, Z` (`super_interfaces` clause)
3. **Field type usage** — a field's declared type referencing another project type
4. **Method calls** — both bare calls (`foo()`) and calls on a receiver (`obj.foo()`), with receiver-type inference
5. **Object creation** — `new SomeType(...)`

### Construction & Entry Point

```python
TreeSitterJavaAnalyzer(file_path, content, repo_path=None)
```
On construction, `__init__` immediately extracts the package name and import table, then runs `_analyze()`, populating `self.nodes` and `self.call_relationships`. The module-level convenience function:

```python
analyze_java_file(file_path, content, repo_path=None) -> (List[Node], List[CallRelationship])
```
is the entry point used by `CallGraphAnalyzer._analyze_java_file`.

### Package & Import Awareness

Unlike the C/C++ analyzers, Java's ID scheme must account for a real module/namespace system. On construction the analyzer:

1. **`_extract_package_name()`** — regex-matches the `package a.b.c;` statement (if any).
2. **`_extract_imports()`** — regex-scans every `import [static] a.b.C[.*];` statement, building:
   - `import_map: dict[str, str]` — simple name → fully-qualified name (e.g. `{"List": "java.util.List"}`), including static imports (`{"checkNotNull": "com.google.common.base.Preconditions.checkNotNull"}`).
   - `wildcard_imports: list[str]` — package prefixes from `import a.b.*;` statements, used only as a fallback signal for JDK-package detection (a project's own wildcard imports fall through to normal resolution).

These two structures power the "qualify a bare type/member name" logic in `_resolve_java_type` / `_resolve_java_member`, described below.

### ID Scheme

- **`component_id`** (via `_get_component_id`): `"<relative_path>::<name>"`, where `<name>` for a method is already `ClassName.methodName` (nested classes are **not** further nested in the component_id path — only `qualified_name` reflects full nesting).
- **`qualified_name`** (via `_qualified_type_name` / `_qualified_member_name`): the fully package-and-outer-class-qualified name, e.g. `com.example.Outer.Inner.method`, built by walking `_find_containing_type_names` (all enclosing class/interface/enum/record/annotation declarations) and prefixing the file's `package_name`.

This dual scheme means `component_id` is used for the graph key (matching the `<relative_path>::<name>` convention shared by all analyzers), while `qualified_name` is the semantically precise, package-qualified name used to resolve cross-references that mention the fully-qualified or partially-qualified form.

### Processing Flow

```mermaid
flowchart TD
    A[Source content] --> PKG["_extract_package_name()"]
    A --> IMP["_extract_imports()<br/>import_map + wildcard_imports"]
    PKG --> B[tree_sitter_java parser]
    IMP --> B
    B --> C[Root AST node]
    C --> D["_extract_nodes(root)"]
    D --> D1[Recursive traversal]
    D1 --> D2{Node type?}
    D2 -->|class_declaration| E1["'class'/'abstract class' Node"]
    D2 -->|interface_declaration| E2["'interface' Node"]
    D2 -->|enum_declaration| E3["'enum' Node"]
    D2 -->|record_declaration| E4["'record' Node"]
    D2 -->|annotation_type_declaration| E5["'annotation' Node"]
    D2 -->|method_declaration| E6["'method' Node<br/>qualified_name via containing types"]
    E1 & E2 & E3 & E4 & E5 & E6 --> F["top_level_nodes map<br/>(keyed by name, component_id, qualified_name, simple qualified suffix)"]
    C --> G["_extract_relationships(root, top_level_nodes)"]
    G --> G1{Node type?}
    G1 -->|superclass| H1[Resolve base class via _resolve_java_type<br/>Emit unresolved CallRelationship]
    G1 -->|super_interfaces| H2[Resolve interface's<br/>Emit unresolved CallRelationship per interface]
    G1 -->|field_declaration| H3[Resolve field type<br/>Emit unresolved CallRelationship]
    G1 -->|method_invocation| H4["Infer receiver type<br/>_resolve_java_member<br/>Emit unresolved CallRelationship"]
    G1 -->|object_creation_expression| H5[Resolve created type<br/>Emit unresolved CallRelationship]
    H1 & H2 & H3 & H4 & H5 --> I[self.call_relationships]
    F --> J[self.nodes]
```

### Type & Member Resolution

The core resolution logic lives in two cooperating methods:

```mermaid
flowchart TD
    subgraph resolve_type["_resolve_java_type(type_name, context_node, top_level_nodes)"]
        T0[Strip generics via _simple_type_name] --> T1{Already dotted?}
        T1 -->|yes| T2[Return as-is]
        T1 -->|no| T3{In import_map?}
        T3 -->|yes| T4[Return imported FQN]
        T3 -->|no| T5["Walk containing_types outward:<br/>try package.Outer.Inner.Type"]
        T5 -->|match in top_level_nodes| T6[Return nested-qualified candidate]
        T5 -->|no match| T7{package_name set?}
        T7 -->|yes| T8[Return package.Type]
        T7 -->|no| T9[Return bare Type]
    end

    subgraph resolve_member["_resolve_java_member(member_name, context_node, top_level_nodes, target_type)"]
        M0{target_type given?} -->|yes| M1["_resolve_java_type target → qualified_type.member"]
        M1 --> M2{candidate in top_level_nodes?}
        M2 -->|yes| M3[Return candidate]
        M2 -->|no| M4["Try simple_type.member fallback"]
        M0 -->|no, bare call| M5[Walk containing_types outward:<br/>try Type.member for each enclosing type]
        M5 -->|found| M6[Return candidate]
        M5 -->|not found| M7{member_name in import_map?<br/>static import}
        M7 -->|yes| M8[Return imported FQN]
        M7 -->|no| M9[Return package.member]
    end
```

### JDK / External-Type Filtering

`_is_primitive_type` and `_skip_type` decide, at relationship-extraction time, whether a referenced type name can *ever* be a project component — avoiding emitting noisy edges to JDK/runtime types or generic type parameters:

- **Java primitives / keywords**: `boolean, byte, char, double, float, int, long, short, void, var` — always skipped.
- **Generic type parameters in scope** (`_find_type_parameters`): walks up through enclosing `class_declaration` / `interface_declaration` / `record_declaration` / `method_declaration` nodes collecting `type_parameters` (e.g. the `K`, `V` of `class Cache<K, V>`), so a field of type `K` isn't misclassified as an external or unresolved project reference.
- **JDK/runtime detection**: the simple type name is resolved through `import_map` (or, for wildcard imports, checked as `wildcard.SimpleName`) to its best-guess fully-qualified form, which is then passed to the shared [`is_external_symbol("java", qualified)`](Dependency_Analyzer_Core.md) classifier. This generalizes JDK filtering to *any* repository without hardcoding a type list — only the `java./javax./jdk./sun.` namespace-prefix rule and the curated `java.lang` set (which has no import statement to consult) are hardcoded.

```mermaid
flowchart LR
    TypeName[type_name] --> Simple["_simple_type_name<br/>strip generics"]
    Simple --> Prim{in primitives set?}
    Prim -->|yes| Skip[Skip: not a component]
    Prim -->|no| Lookup["import_map.get(simple)"]
    Lookup -->|found| Qualified[qualified FQN]
    Lookup -->|not found| Wild{matches a wildcard<br/>import package?}
    Wild -->|yes, is_external_symbol true| Skip
    Wild -->|no| Qualified2[qualified = simple]
    Qualified --> ExtCheck["is_external_symbol java qualified"]
    Qualified2 --> ExtCheck
    ExtCheck -->|True| Skip
    ExtCheck -->|False| Keep[Treat as potential project type<br/>emit CallRelationship]
```

### Method-Call Receiver-Type Inference

For `method_invocation` nodes (`obj.method()` or bare `method()`), the analyzer must guess what `obj`'s static type is in order to resolve `method` to a specific class member:

1. If the receiver identifier starts with an uppercase letter and is itself a known top-level type name → treat as a **static call** on that type.
2. Otherwise, call `_find_variable_type` — scans the enclosing method/constructor body (`local_variable_declaration`s, recursively through nested `block`s) and its parameter list, falling back to the enclosing class's `field_declaration`s, to find where `obj` was declared and what type annotation it carries.
3. If no declaration is found but the receiver is CamelCase (and not `ALL_CAPS`, which reads as a constant) → assume it's still a type reference (e.g. an imported class used for a static call CodeWiki didn't track as a local variable).
4. Bare calls with no receiver either resolve against enclosing types (walking outward through nested classes) or, if the callee corresponds to a static import, resolve directly via `import_map`.

A special guard: if the finally-resolved callee isn't a known project component and the method name is one of the well-known `Object` methods (`JAVA_OBJECT_METHODS`, e.g. `toString`, `equals`, `hashCode`), the call is dropped entirely rather than emitted as an unresolved edge — since an inherited `Object` method with no local override is never a meaningful project-internal edge to surface.

---

## `TreeSitterKotlinAnalyzer` (kotlin.py)

### Purpose

Parses a single `.kt`/`.kts` file using the `tree_sitter_kotlin` grammar and extracts:
- **Classes** (`class_declaration`), refined by modifier inspection into `class`, `abstract class`, `data class`, `enum class`, or `annotation class`
- **Interfaces** (`class_declaration` containing an `interface` child token)
- **Objects** (`object_declaration`) — Kotlin's singleton declaration form
- **Functions and Methods** (`function_declaration`) — qualified as `ClassName.methodName` when nested in a class/object, otherwise a free `function`

It records relationships for:
1. **Inheritance / interface implementation** via `delegation_specifiers` (Kotlin's unified `: Base(), Interface1, Interface2` syntax, including delegated constructor invocations)
2. **Property type usage** (`property_declaration` with an explicit `user_type` annotation)
3. **Constructor parameter type usage** (`class_parameter` in a primary constructor)
4. **Function/method calls** (`call_expression`), including receiver-qualified calls via `navigation_expression` (`obj.method()`)

Unlike the Java analyzer, Kotlin's analyzer does **not** build a package/import table — Kotlin source commonly omits explicit imports for same-package types, and the analyzer instead relies on simple-name matching against `top_level_nodes` plus a curated primitive/built-in filter.

### Construction & Entry Point

```python
TreeSitterKotlinAnalyzer(file_path, content, repo_path=None)
analyze_kotlin_file(file_path, content, repo_path=None) -> (List[Node], List[CallRelationship])
```
`_analyze()` wraps parsing in a `try/except`, logging and continuing gracefully (producing empty `nodes`/`call_relationships`) if the Kotlin grammar cannot parse the file — a defensive measure the Java analyzer does not need since Java's grammar is generally more forgiving of this codebase's inputs.

### ID Scheme

- **`component_id`** (`_get_component_id`): `"<relative_path>::<name>"`, where a method's `<name>` is already `ClassName.methodName` (mirrors the Java analyzer's flat, non-nested scheme).
- Unlike Java, Kotlin `Node` objects do **not** populate `qualified_name` or `language` — relationship targets are simple/qualified-simple names, left for `CallGraphAnalyzer` to resolve against the repository-wide `component_id`/name indexes.

### Class Modifier Refinement

`_get_class_modifiers` walks a `class_declaration`'s `modifiers` child, collecting inner tokens from `class_modifier` / `inheritance_modifier` / `visibility_modifier` groups (e.g. `abstract`, `data`, `enum`, `annotation`). `_extract_nodes` uses this set — together with a direct check for an `interface` child token — to assign the most specific `node_type`:

```mermaid
flowchart TD
    CD[class_declaration] --> HasIface{has 'interface' child?}
    HasIface -->|yes| Iface["'interface'"]
    HasIface -->|no| Mods["_get_class_modifiers"]
    Mods --> M1{'abstract' in modifiers?}
    M1 -->|yes| Abs["'abstract class'"]
    M1 -->|no| M2{'data' in modifiers?}
    M2 -->|yes| Data["'data class'"]
    M2 -->|no| M3{'enum' in modifiers?}
    M3 -->|yes| Enum["'enum class'"]
    M3 -->|no| M4{'annotation' in modifiers?}
    M4 -->|yes| Anno["'annotation class'"]
    M4 -->|no| Plain["'class'"]
```

### Processing Flow

```mermaid
flowchart TD
    A[Source content] --> B["tree_sitter_kotlin parser<br/>wrapped in try/except"]
    B --> C[Root AST node]
    C --> D["_extract_nodes(root)"]
    D --> D1{Node type?}
    D1 -->|class_declaration| E1["class/interface/abstract/data/enum/annotation Node"]
    D1 -->|object_declaration| E2["'object' Node"]
    D1 -->|function_declaration| E3["'method' or 'function' Node<br/>+ docstring from preceding comment sibling"]
    E1 & E2 & E3 --> F[top_level_nodes map<br/>keyed by simple name]
    C --> G["_extract_relationships(root, top_level_nodes)"]
    G --> G1{Node type?}
    G1 -->|class_declaration| H1["delegation_specifiers to base/interface CallRelationship's"]
    G1 -->|property_declaration| H2["user_type field to CallRelationship"]
    G1 -->|class_parameter| H3["constructor param type to CallRelationship"]
    G1 -->|call_expression| H4["identifier or navigation_expression<br/>receiver-type inference to CallRelationship"]
    H1 & H2 & H3 & H4 --> I[self.call_relationships]
    F --> J[self.nodes]
```

### Call-Expression Resolution

`call_expression` handling branches on the shape of the invoked target:

```mermaid
flowchart TD
    CE[call_expression] --> Target{target: identifier<br/>or navigation_expression?}
    Target -->|"identifier, e.g. Foo()"| Simple{First letter uppercase?}
    Simple -->|yes| ClassCtor["Treat as class instantiation<br/>CallRelationship caller to component_id Name"]
    Simple -->|"no, e.g. topLevelFn()"| FreeFn["CallRelationship caller to name<br/>left for CallGraphAnalyzer to resolve"]

    Target -->|"navigation_expression, e.g. obj.method()"| NavParse["Collect identifiers in expression:<br/>2+ identifiers to object, method<br/>1 identifier + nested nav via _get_root_identifier"]
    NavParse --> HasObj{object + method both found?}
    HasObj -->|yes| TypeGuess["object_name in top_level_nodes?<br/>else _find_variable_type"]
    TypeGuess -->|resolved, non-primitive| Rel1["CallRelationship caller to component_id target_type"]
    HasObj -->|no, method only| Rel2["CallRelationship caller to method_name"]
```

`_find_variable_type` mirrors the Java analyzer's scope-walking strategy, but adapted to Kotlin constructs:
1. **Function parameters** (`function_value_parameters` → `parameter` nodes with a `user_type`/`nullable_type` annotation).
2. **Local variable declarations** inside the function body (`_search_variable_declaration`, recursing through nested `block`s) — including a light type-inference fallback: if a `property_declaration` has no explicit type but its initializer is a `call_expression` to an uppercase-named function, that name is inferred as the type (covers `val x = Foo()` constructor-call idiom).
3. **Primary constructor parameters** (`primary_constructor` → `class_parameters` → `class_parameter`).
4. **Class-body properties** (`class_body`/`enum_class_body` → `property_declaration`).

### Built-in Type Filtering

`_is_primitive_type` uses a curated, hardcoded set of Kotlin primitive and standard-collection type names (`Boolean, Byte, ..., String, Unit, Nothing, Any, List, Set, Map, MutableList, ..., Array, IntArray, ..., Pair, Triple`) to suppress edges to obvious built-ins. This is simpler than the Java analyzer's import-aware `is_external_symbol` approach — Kotlin's analyzer does not currently attempt to resolve arbitrary `kotlin.*`/`kotlinx.*` stdlib types beyond this fixed list, so uncommon stdlib types may still produce unresolved edges that `CallGraphAnalyzer`'s downstream filtering must catch.

---

## Java vs. Kotlin: Design Comparison

| Aspect | `TreeSitterJavaAnalyzer` | `TreeSitterKotlinAnalyzer` |
|---|---|---|
| Package/import table | Yes — `package_name`, `import_map`, `wildcard_imports` parsed via regex | No — resolution is purely structural/local |
| Type qualification | `qualified_name` field set on every `Node`, package + outer-class qualified | Not set (`None`) — relies on simple names |
| External-symbol filtering | Delegates to shared `is_external_symbol("java", ...)` after import-based qualification | Curated fixed primitive/collection set only |
| Generic type parameters | Explicitly tracked and excluded (`_find_type_parameters`) | Not explicitly tracked |
| Inheritance/implements syntax | Separate `superclass` and `super_interfaces` clauses | Unified `delegation_specifiers` (Kotlin has one syntax for both) |
| Class variants | `class`, `abstract class`, `interface`, `enum`, `record`, `annotation` | `class`, `abstract class`, `data class`, `enum class`, `annotation class`, `interface`, `object` |
| Parse-failure handling | No explicit guard (grammar assumed to succeed) | Wrapped in `try/except`, logs and continues with empty results |
| Docstrings | Not captured (`has_docstring=False` always) | Captured from an immediately preceding `line_comment`/`block_comment` sibling |

Both analyzers, despite these differences, converge on the same `Node`/`CallRelationship` output contract described in [C-Family_Tree-sitter_Analyzers](C-Family_Tree-sitter_Analyzers.md) and [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md), which is what allows `CallGraphAnalyzer` to treat all five C-family analyzers uniformly during cross-file resolution.

---

## Integration Points

- **Invocation**: `CallGraphAnalyzer._analyze_java_file` / `_analyze_kotlin_file` (see [Dependency_Analysis_Service](Dependency_Analysis_Service.md)) call `analyze_java_file` / `analyze_kotlin_file` per discovered source file (file discovery itself is handled by `RepoAnalyzer`).
- **Output consumption**: the returned `(nodes, call_relationships)` tuples are merged with every other language analyzer's output into `CallGraphAnalyzer`'s repository-wide indexes, where unresolved (`is_resolved=False`) edges are matched against known component IDs/qualified names, and any edge that still can't be matched is checked against `_is_external_callee` before being classified as either a real cross-file dependency or discarded as external/unknown.
- **Downstream**: resolved `Node`/`CallRelationship` data feeds [`DependencyParser`](Dependency_Analyzer_Core.md) and [`DependencyGraphBuilder`](Dependency_Analyzer_Core.md), which produce the dependency graph consumed by [documentation generation](Backend_LLM_&_Documentation_Services_documentation_generator.md).
- **Shared utility dependency**: `TreeSitterJavaAnalyzer` imports `JAVA_OBJECT_METHODS` and `is_external_symbol` from `codewiki.src.be.dependency_analyzer.utils.external_symbols` — the same module the C/C++ analyzers use for their own language-specific external-symbol sets (see [C-Family_Tree-sitter_Analyzers_C_Cpp](C-Family_Tree-sitter_Analyzers_C_Cpp.md) for that module's parallel usage).
