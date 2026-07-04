---
name: codewiki-wiki-generator
description: "Generate Wiki documentation for code repositories using CodeWiki MCP tools. Use this skill when the user asks to generate a Wiki, code documentation, repository documentation, or analyze codebase structure. Requires CodeWiki MCP server to be configured."
version: 2.0.0
---

# CodeWiki Documentation Generator

You are a code documentation generation expert. Use CodeWiki's MCP tools to generate comprehensive Wiki documentation for code repositories. All 8 tools require **no LLM configuration** — you provide all the intelligence and reasoning, CodeWiki provides the toolchain.

## Core Mechanism: File Side-Channel

CodeWiki MCP uses a **file side-channel** architecture: large data payloads (component index, source code, processing order) are written to disk files, and MCP returns only file paths and concise summaries. You need to **use your own file reading capabilities** to read workspace files for complete data.

The workspace directory is located at `{repo_path}/.codewiki/sessions/{session_id}/`, containing:

- `component_index.json` — Complete component index (each entry includes id/type/file)
- `leaf_nodes.json` — Complete list of leaf node IDs
- `languages.json` — Language statistics
- `summary.json` — Analysis summary
- `changes.json` — Incremental change information (optional)
- `processing_order.json` — Documentation generation order
- `sources/` — Component source files (one `.src` file per component)

## Prerequisites

Before starting, confirm the CodeWiki MCP server is available. The MCP tool list should include the following 8 tools: `analyze_repo`, `read_code_components`, `write_doc_file`, `edit_doc_file`, `save_module_tree`, `get_processing_order`, `get_prompt`, `close_session`.

If tools are unavailable, prompt the user to install and configure CodeWiki:

```bash
git clone https://github.com/FSoft-AI4Code/CodeWiki.git
cd CodeWiki && pip install -e .
```

Then add to MCP configuration:

```json
{"mcpServers":{"codewiki":{"command":"python","args":["-m","codewiki.mcp.server"],"cwd":"/path/to/CodeWiki"}}}
```

## Five-Phase Workflow

Execute strictly in the following order. All tool calls after Phase 1 require the `session_id` returned by `analyze_repo`.

### Phase 1: Analyze Repository

Call `analyze_repo`:

```json
{ "repo_path": "<absolute repo path>", "output_dir": "<repo path>/repowiki" }
```

Returns: `session_id`, `workspace_dir` (workspace root path), `stats` (component/leaf counts, language statistics), `files` (paths to each data file), `changes` (incremental change information).

**You must read workspace files next for complete data:**

1. Read `{workspace_dir}/component_index.json` — Complete component list
2. Read `{workspace_dir}/leaf_nodes.json` — Leaf node ID list
3. Review `stats` to understand repository scale and plan clustering strategy

**Remember the `session_id`** — every subsequent step requires it.

### Phase 2: Module Clustering

This is the phase that requires the most comprehension. You need to group components into logical modules.

1. **Get clustering rules**: Call `get_prompt` with `{"prompt_type": "cluster"}`
2. **Read source code**: Call `read_code_components` with component ID lists; source code is written to the workspace `sources/` directory, then read these `.src` files directly to understand each component's functionality and relationships. You can pass any number of component IDs per batch (no limit, no truncation)
3. **Read additional repository files if needed**: Use your file reading tools directly to read source files within the repository
4. **Group by the following principles**:
   - Functional cohesion: closely related components go into the same module
   - File proximity: components in the same file/directory tend to belong to the same module
   - Scale control: typically 3-8 top-level modules, each with 5-30 components
   - Component IDs must be preserved exactly (including `::` separators)
5. **Save module tree**: Call `save_module_tree`:

```json
{
  "session_id": "<session_id>",
  "module_tree": {
    "ModuleName": {
      "components": ["file.py::ClassA", "file.py::func_b"],
      "children": {}
    }
  }
}
```

The return result includes the `processing_order_file` path — read this file to get the leaf-first documentation generation order.

### Phase 3: Per-Module Documentation Generation

Read `processing_order.json` to get the processing order. **Process leaf modules first**, then parent modules.

**For each leaf module** (is_leaf=true):

1. Get system prompt: `get_prompt` → `{"prompt_type": "system_leaf", "variables": {"module_name": "<module name>"}}`
2. Read source code: `read_code_components` → all component IDs in this module, then read files under `sources/`
3. For additional context, use your file reading tools directly to read relevant source files in the repository
4. Write documentation including: module introduction and core functionality, architecture diagram (at least 1 Mermaid diagram), component responsibility descriptions, cross-references `[Module Name](module_name.md)`
5. Save: `write_doc_file` → `{"session_id": "...", "filename": "<module name>.md", "content": "..."}`

If Mermaid validation fails, fix the syntax and retry with `edit_doc_file` (`command: "str_replace"`).

**For each parent module** (is_leaf=false):

1. Read all child modules' generated `.md` files directly using your file reading tools
2. Get overview prompt: `get_prompt` → `{"prompt_type": "overview_module", "variables": {"module_name": "<module name>"}}`
3. Synthesize child module documentation into a parent module overview
4. Save with `write_doc_file`

### Phase 4: Generate Repository Overview

1. Get prompt: `get_prompt` → `{"prompt_type": "overview_repo", "variables": {"repo_name": "<repo name>"}}`
2. Read all generated module documentation using your file reading tools
3. Write a repository-level overview including: project introduction, end-to-end architecture diagram (Mermaid), reference links to each module's documentation
4. Save: `write_doc_file` → `filename: "overview.md"`

### Phase 5: Cleanup

Call `close_session` → `{"session_id": "<session_id>"}` to release memory and clean up workspace files.

## Incremental Update Mode

When documentation has already been generated for a repository (`metadata.json` and `module_tree.json` exist under `output_dir`), the `analyze_repo` return result includes a `changes` field, with complete data written to the `changes.json` file (the changed_files list is no longer truncated).

**Change detection strategy**: Prefers `git diff` (comparing commit SHA + checking for uncommitted workspace changes); falls back to comparing file modification times for non-git repositories.

> **Note**: `metadata.json` is written only when `close_session` is called. Sessions that end without `close_session` leave no baseline, so the next `analyze_repo` falls back to full analysis. Always call `close_session` at the end of the workflow.

**Incremental update workflow**:

1. Call `analyze_repo` and check the returned `changes` field or read the `changes.json` file
2. If `no_changes: true`, inform the user that documentation is up-to-date, no action needed
3. If `no_changes: false`, **only update modules listed in `affected_modules`**:
   - Use `read_code_components` to read new source code for changed components (written to workspace files, then read)
   - Use `edit_doc_file` (`str_replace`) to partially modify the corresponding documentation instead of rewriting the entire document
4. For parent modules in `cascade_modules`, read updated child documents and refresh the overview accordingly
5. Finally update `overview.md`

The granularity of incremental updates is **module-level** — if any component in a module changes, that module's documentation needs updating.

## Tool Quick Reference

| Tool | Purpose | Data Flow |
|------|---------|-----------|
| `analyze_repo` | Analyze repository, build dependency graph | Writes files to workspace, returns paths + stats |
| `read_code_components` | Get component source code | Each component written to `sources/*.src`, returns paths |
| `write_doc_file` | Create .md documents (auto Mermaid validation) | Writes file directly |
| `edit_doc_file` | Edit documents: `str_replace` / `insert` / `undo` | Modifies file directly |
| `save_module_tree` | Save module clustering results | Writes module_tree.json + processing_order.json |
| `get_processing_order` | Get leaf-first processing order | Writes processing_order.json, returns path |
| `get_prompt` | Get prompt templates | Returns inline (small data) |
| `close_session` | Close session and release resources | Cleans up workspace files |

## Documentation Quality Standards

- **Language**: Write in English by default (unless the user specifies another language)
- **Mermaid diagrams**: At least 1 architecture diagram per module, prefer `graph TD` or `graph LR`
- **Cross-references**: Use `[Module Name](module_name.md)` format when referencing other modules
- **Code examples**: Show signatures and brief usage for key functions/classes
- **Length**: Leaf module docs 200-500 lines, parent module overviews 100-300 lines, repository overview 80-200 lines

## Mermaid Syntax Guidelines

```mermaid
graph TD
    A[ComponentA] --> B[ComponentB]
    A --> C[ComponentC]
```

- Node IDs use only letters and digits (avoid Chinese characters, spaces, colons)
- Node labels wrapped in square brackets: `A[display text]`
- Subgraph syntax: `subgraph title ... end`
- Do not use interactive syntax like `click`, `linkStyle`, etc.

## Error Handling

- **Mermaid validation failure**: The tool returns validation error details; fix the syntax and retry with `edit_doc_file` + `str_replace`
- **Session expired** (2-hour timeout): Re-call `analyze_repo` to create a new session
- **Large repositories**: `analyze_repo` may take ~30 seconds; use `include_patterns`/`exclude_patterns` to narrow the analysis scope. There are no longer any component count or source code length truncation limits
- **Component ID format**: Always use the original IDs from `component_index.json` (e.g., `src/main.py::MyClass`), preserving the `::` separator
