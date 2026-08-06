# Backend LLM & Documentation Services

## Purpose

This module is the **engine room of CodeWiki's documentation generation**. It
owns:

1. **The LLM backend abstraction** — a single interface (`LLMBackend`) that
   lets the rest of the system call an LLM without caring whether the call
   goes through an API key (OpenAI-compatible / Anthropic / Bedrock /
   Azure-OpenAI via pydantic-ai) or through the user's Claude/Codex CLI
   subscription (via the `caw` library).
2. **The top-level orchestrator** (`DocumentationGenerator`) that turns a
   dependency graph + module tree into a finished set of markdown pages —
   clustering modules, walking the tree leaves-first, running the per-module
   agent loop, and stitching parent/repo overviews on top.

Everything upstream of this module (dependency graph construction, module
clustering input, agent tool implementations) is documented elsewhere and
linked below; this module is the piece that actually *drives the agents* and
decides *which backend* runs them.

## How this module fits in the system

```mermaid
flowchart TD
    subgraph Entry["Entry points"]
        CLI["CLI\n(Backend_LLM_&_Documentation_Services is invoked via CLIDocumentationGenerator)"]
        FE["Frontend Web App\n(background_worker.py)"]
    end

    subgraph ThisModule["Backend_LLM_&_Documentation_Services"]
        DG["DocumentationGenerator"]
        BK["LLMBackend (interface)\nget_backend()"]
        PA["PydanticAIBackend"]
        CB["CawBackend"]
        CT["CawToolKit (MCP server)"]
        LS["llm_services\n(CompatibleOpenAIModel, CachingOpenAIModel)"]
    end

    subgraph Deps["Depended-on modules"]
        DAC["Dependency_Analyzer_Core\n(DependencyGraphBuilder, Node, AnalysisResult)"]
        DAS["Dependency_Analysis_Service\n(AnalysisService, RepoAnalyzer)"]
        AT["Backend_Agent_Tools\n(CodeWikiDeps, EditTool, str_replace_editor)"]
        CFG["Core_Config_&_Utils\n(Config, FileManager)"]
    end

    CLI --> DG
    FE --> DG
    DG --> DAC
    DAC --> DAS
    DG --> BK
    BK --> PA
    BK --> CB
    PA --> LS
    PA --> AT
    CB --> CT
    CT --> AT
    DG --> CFG
    PA --> CFG
    CB --> CFG
```

* [CLI](CLI.md) invokes documentation generation through
  `CLIDocumentationGenerator`, which ultimately constructs and runs a
  `DocumentationGenerator` from this module.
* [Frontend_Web_App](Frontend_Web_App.md) does the same from
  `background_worker.py` for web-submitted jobs.
* [Dependency_Analyzer_Core](Dependency_Analyzer_Core.md) supplies the
  `DependencyGraphBuilder`, `Node`/`Repository` models, and the module-tree
  JSON files (`module_tree.json`, `first_module_tree.json`) this module reads
  and writes.
* [Dependency_Analysis_Service](Dependency_Analysis_Service.md) and
  [Language_Analyzers](Language_Analyzers.md) sit underneath the graph
  builder and are not called directly from here.
* [Backend_Agent_Tools](Backend_Agent_Tools.md) provides `CodeWikiDeps` (the
  per-agent-run state object) and the `EditTool`/`str_replace_editor`
  implementation that both backends reuse for file writes and Mermaid
  validation.
* [Core_Config_&_Utils](Core_Config_&_Utils.md) provides the `Config` object
  (provider, model names, thresholds, paths) and `FileManager` for JSON/text
  I/O, used throughout this module.
* [MCP_Session_Management](MCP_Session_Management.md) is unrelated to LLM
  calls; it manages per-session workspaces for the MCP server front-end and
  is not a dependency of this module.

## Sub-modules

| Sub-module | Documentation | Responsibility |
|---|---|---|
| API-key backend & model layer | [Backend_LLM_&_Documentation_Services_pydantic_ai_backend.md](Backend_LLM_&_Documentation_Services_pydantic_ai_backend.md) | `LLMBackend` interface, provider selection (`get_backend`), `PydanticAIBackend` (pydantic-ai `Agent` + tool functions), and the `llm_services` model layer (`CompatibleOpenAIModel`, `CachingOpenAIModel`, fallback-model construction) |
| Subscription (caw) backend | [Backend_LLM_&_Documentation_Services_caw_backend.md](Backend_LLM_&_Documentation_Services_caw_backend.md) | `CawBackend` (routes through the `claude`/`codex` CLI) and `CawToolKit` (the MCP tool server exposing CodeWiki's tools to a caw agent) |
| Documentation orchestration | [Backend_LLM_&_Documentation_Services_documentation_generator.md](Backend_LLM_&_Documentation_Services_documentation_generator.md) | `DocumentationGenerator`: clustering trigger, leaf-first processing order, per-module agent dispatch, parent/repo overview assembly, and post-run validation |

## Architecture overview

### Backend selection

`get_backend(config)` is the single seam where provider choice becomes a
concrete class. Everything else in the codebase (the orchestrator, the
clustering step) talks only to the abstract `LLMBackend` interface.

```mermaid
classDiagram
    class LLMBackend {
        <<abstract>>
        +complete(prompt, model=None) str
        +run_module_agent(module_name, components, core_component_ids, module_path, working_dir) dict
    }
    class PydanticAIBackend {
        -_fallback_models: FallbackModel
        +complete(prompt, model=None) str
        +run_module_agent(...) dict
    }
    class CawBackend {
        -_caw_provider: str
        -_model: str
        +complete(prompt, model=None) str
        +run_module_agent(...) dict
        -_run_module_agent_sync(...) dict
    }
    LLMBackend <|-- PydanticAIBackend
    LLMBackend <|-- CawBackend

    class get_backend {
        <<function>>
    }
    get_backend --> LLMBackend : returns
```

* `provider in {"claude-code", "codex"}` (`is_caw_provider`) → `CawBackend`
* every other provider value (`openai-compatible`, `anthropic`, `bedrock`,
  `azure-openai`) → `PydanticAIBackend`

See [Backend_LLM_&_Documentation_Services_pydantic_ai_backend.md](Backend_LLM_&_Documentation_Services_pydantic_ai_backend.md)
and [Backend_LLM_&_Documentation_Services_caw_backend.md](Backend_LLM_&_Documentation_Services_caw_backend.md)
for the two implementations.

### End-to-end generation flow

```mermaid
sequenceDiagram
    participant Caller as CLI / Web worker
    participant DG as DocumentationGenerator
    participant CM as cluster_modules
    participant Backend as LLMBackend
    participant FS as FileManager (module_tree.json, *.md)

    Caller->>DG: run()
    DG->>DG: graph_builder.build_dependency_graph()
    alt no cached module tree
        DG->>CM: cluster_modules(leaf_nodes, components, completer=backend.complete)
        CM-->>DG: module_tree
        DG->>FS: save first_module_tree.json / module_tree.json
    end
    DG->>DG: get_processing_order() (leaf modules first)
    loop each module, leaves before parents
        alt leaf module
            DG->>Backend: run_module_agent(module_name, core_component_ids, ...)
            Backend-->>FS: writes {module_name}.md, updates module_tree.json
        else parent module
            DG->>Backend: complete(MODULE_OVERVIEW_PROMPT)
            Backend-->>FS: writes {module_name}.md
        end
    end
    DG->>Backend: complete(REPO_OVERVIEW_PROMPT)
    Backend-->>FS: writes overview.md
    DG->>DG: validate_generated_docs() -> raise IncompleteDocumentationError if any .md missing
```

Full detail on this flow lives in
[Backend_LLM_&_Documentation_Services_documentation_generator.md](Backend_LLM_&_Documentation_Services_documentation_generator.md).

### Per-module agent loop, both backends

Both backends implement the same contract (`run_module_agent`) but drive it
with different agent runtimes and different tool-call surfaces:

```mermaid
flowchart LR
    subgraph PydanticAIBackend
        A1["pydantic_ai.Agent\n+ CachingOpenAIModel / FallbackModel"] --> A2["tools: read_code_components_tool,\nstr_replace_editor_tool,\ngenerate_sub_module_documentation_tool"]
    end
    subgraph CawBackend
        B1["caw.Agent\n(claude / codex CLI)"] --> B2["CawToolKit (MCP server):\nread_code_components,\nstr_replace_editor,\ngenerate_sub_module_documentation"]
    end
    A2 -. same tool contract .- B2
```

Both tool surfaces ultimately call into
[Backend_Agent_Tools](Backend_Agent_Tools.md) (`CodeWikiDeps`, `EditTool`)
and recurse into sub-modules using the same `is_complex_module` /
`max_token_per_leaf_module` / `max_depth` gating logic, so a module produces
equivalent documentation regardless of which backend generated it.

## Key cross-cutting concerns

- **Module tree as shared state**: `module_tree.json` (and the initial
  `first_module_tree.json` snapshot from clustering) is the single source of
  truth for hierarchy. Both backends load/mutate/save it, and sub-agent
  delegation (`generate_sub_module_documentation`) adds new branches to it
  in-memory before persisting. Name collisions across the (flat) docs
  directory are resolved by `module_naming.normalize_sub_module_specs` /
  `dedupe_module_tree_names`.
- **Recursion budget**: `config.max_depth` and `config.max_token_per_leaf_module`
  gate whether a module can delegate to sub-agents (`is_complex_module`
  + token count), preventing runaway fan-out on deeply nested repos.
- **Idempotency**: every entry point checks for an existing `overview.md` or
  `{module_name}.md` before doing any work, so a crashed run can be resumed
  by re-invoking generation over the same `docs_dir`.
- **Validation**: `DocumentationGenerator.validate_generated_docs` /
  `IncompleteDocumentationError` guarantee that a "successful" run actually
  produced every promised `.md` file, not just an updated tree in memory.

Configuration (`Config`, provider/model fields, thresholds) is defined in
[Core_Config_&_Utils](Core_Config_&_Utils.md); the underlying source-analysis
data structures (`Node`, `Repository`, `AnalysisResult`) come from
[Dependency_Analyzer_Core](Dependency_Analyzer_Core.md).
