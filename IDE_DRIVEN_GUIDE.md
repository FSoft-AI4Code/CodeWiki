# CodeWiki IDE-Driven Mode: Refactoring Process & Usage Guide

## Background & Motivation

The original CodeWiki design required users to configure their own LLM API (API Key + base_url), then generate documentation via a one-shot CLI command. This introduced two problems:

1. **Configuration barrier**: Users need to obtain API Keys, understand provider differences, and handle model compatibility issues
2. **Inflexibility**: The generation process is a black box — users cannot intervene in clustering strategies or documentation style during generation

**Refactoring goal**: Reduce CodeWiki to a **pure toolchain MCP Server**, fully driven by AI IDE agents (CodeBuddy, Cursor, etc.) to execute the Wiki generation pipeline with **zero LLM configuration**.

---

## Refactoring Process

### Architecture Analysis

Through source code analysis, CodeWiki's Wiki generation pipeline depends on LLM in 4 stages:

| Stage | Code Location | Invocation | LLM Role |
|-------|---------------|------------|----------|
| Module clustering | `cluster_modules.py` | `backend.complete()` | Group components into logical modules |
| Per-module documentation | `pydantic_ai_backend.py` | `agent.run()` multi-turn | Read code, write docs, draw Mermaid diagrams |
| Sub-module recursion | `generate_sub_module_documentations.py` | Sub-agent loop | Recursively handle nested modules |
| Parent module overview | `documentation_generator.py` | `backend.complete()` | Synthesize overviews from child documents |

Key finding: **dependency analysis (Tree-sitter AST parsing), dependency graph construction, topological sorting, and Mermaid validation** — the core toolchain — requires no LLM at all.

### Refactoring Strategy

Transform the MCP Server from "black-box one-shot generation" into a "fine-grained toolset":

```
Before refactoring:
  IDE → generate_docs(repo) → [CodeWiki internally calls LLM] → result

After refactoring:
  IDE Agent → analyze_repo → read_code → (Agent reasons clustering) → write_doc → overview
              ↑ Pure tool call   ↑ Pure tool call  ↑ IDE's own LLM     ↑ Pure tool call
```

### File Side-Channel Architecture

A key design decision in the refactoring: instead of transmitting large payloads (component indexes, source code, processing order) through the MCP stdio channel — which required aggressive truncation and caused overflow errors — the server writes all bulky data to **per-session workspace files** on disk. The MCP response returns only file paths and a compact summary. The IDE agent then reads those files directly using its own file-access capabilities.

This approach eliminates truncation limits entirely: component indexes, source code files, and processing orders are written in full, no matter how large the repository.

### New File Inventory

```
codewiki/mcp/
├── server.py                  # Refactored: 10 tool registrations (8 fine-grained + 2 legacy)
├── session.py                 # Session state management (SessionStore, thread-safe)
├── workspace.py               # Per-session file workspace (write/read/cleanup)
└── tools/
    ├── __init__.py            # Tool package entry point
    ├── analysis.py            # analyze_repo with incremental change detection
    ├── code_reader.py         # read_code_components (writes .src files to workspace)
    ├── doc_writer.py          # write_doc_file + edit_doc_file (with path traversal guards)
    ├── module_tree.py         # save_module_tree + get_processing_order
    └── prompt_server.py       # get_prompt template service
```

### MCP Toolset

The server exposes **8 fine-grained tools** (zero LLM config) plus **2 legacy tools**:

| Tool | Purpose | Data Flow | Requires LLM |
|------|---------|-----------|:---:|
| `analyze_repo` | Parse repo, build dependency graph, detect incremental changes | Writes workspace files (component index, leaf nodes, languages, changes), returns paths + stats | No |
| `read_code_components` | Write component source code to workspace `.src` files | Each component → `sources/{sanitized_id}.src`, returns file paths | No |
| `write_doc_file` | Create .md documents with auto Mermaid validation | Writes file directly to output dir | No |
| `edit_doc_file` | Edit documents: `str_replace` / `insert` / `undo` | Modifies file in place, keeps edit history (capped at 20/file) | No |
| `save_module_tree` | Persist IDE agent's module clustering | Writes `module_tree.json` + `first_module_tree.json` + `processing_order.json` | No |
| `get_processing_order` | Compute leaf-first processing order | Writes `processing_order.json` to workspace, returns path | No |
| `get_prompt` | Retrieve prompt templates for each pipeline stage | Returns inline (small payload) | No |
| `close_session` | Write `metadata.json`, clean up workspace files, free memory | Cleans workspace dir + prunes empty parent dirs | No |
| `generate_docs` | [Legacy] One-shot generation (requires `codewiki config set`) | Full pipeline | **Yes** |
| `get_module_tree` | [Legacy] Get existing module clustering tree | Reads from disk | No |

### Thread Safety & Concurrency

Synchronous tool handlers (file I/O, Tree-sitter parsing) run via `asyncio.to_thread()` to prevent blocking the MCP stdio event loop. The exception is `analyze_repo` — Tree-sitter C extensions are not thread-safe, so it runs on the main thread (acceptable for a one-time heavy operation).

Session management is fully thread-safe: `SessionStore` uses a mutex lock for all read/write operations, supports up to **10 concurrent sessions** (oldest evicted at capacity), and sessions auto-expire after **2 hours** of inactivity.

### Security Hardening

The `doc_writer` module enforces path traversal guards: all file paths are resolved and verified to stay within the session's `output_dir`. Filenames that attempt directory escape are rejected. Edit operations are tracked in session-scoped history (capped at 20 entries per file to prevent unbounded memory growth).

### Backward Compatibility

- Existing CLI (`codewiki generate`, `codewiki config`) remains completely unchanged
- Existing Web App remains completely unchanged
- Legacy MCP tools (`generate_docs`, `get_module_tree`) are preserved — users with configured LLMs can still use them
- The `codewiki/__init__.py` unconditional CLI import was removed, so MCP Server can now start without installing CLI-specific dependencies (`keyring`, `click`, etc.)

---

## Usage

### Prerequisites

```bash
# 1. Clone the project
git clone https://github.com/FSoft-AI4Code/CodeWiki.git
cd CodeWiki

# 2. Install dependencies
pip install -e .

# 3. Verify
python -c "from codewiki.mcp.server import server; print('MCP Server OK')"
```

### CodeBuddy Configuration

**Step 1**: Configure the MCP Server in CodeBuddy.

Add to CodeBuddy's MCP configuration:

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "python",
      "args": ["-m", "codewiki.mcp.server"],
      "cwd": "/path/to/CodeWiki"
    }
  }
}
```

**Step 2**: Project rules are automatically configured in `.codebuddy/rules/codewiki-wiki-generator/RULE.mdc`. When you mention "generate documentation" or "Wiki" in Agent mode, CodeBuddy automatically loads this rule.

**Step 3**: Open CodeBuddy Agent mode and enter:

```
Analyze this repository and generate Wiki documentation for me
```

### Cursor Configuration

**Step 1**: Add the Server in Cursor Settings → MCP:

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "python",
      "args": ["-m", "codewiki.mcp.server"],
      "cwd": "/path/to/CodeWiki"
    }
  }
}
```

**Step 2**: Project rules are configured in `.cursorrules` and automatically loaded when Cursor opens the project.

**Step 3**: In Cursor Agent mode, enter:

```
Please generate Wiki documentation for the current repository, output to the docs directory.
```

### Claude Desktop Configuration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "python",
      "args": ["-m", "codewiki.mcp.server"],
      "cwd": "/path/to/CodeWiki"
    }
  }
}
```

### Other MCP-Capable IDEs

Any AI IDE supporting the MCP stdio protocol can be used with similar configuration — specify `command: python`, `args: ["-m", "codewiki.mcp.server"]`.

---

## IDE Agent Workflow

When you trigger Wiki generation in an AI IDE, the Agent works through the following 5 phases:

```
Phase 1: analyze_repo
  │  → Get session_id, workspace_dir, stats, file paths
  │  → Read workspace files: component_index.json, leaf_nodes.json, languages.json
  │
Phase 2: get_prompt("cluster") + read_code_components + save_module_tree
  │  → Agent reasons independently, groups components into 3-8 logical modules
  │  → Source code written to workspace sources/ dir, agent reads .src files directly
  │  → Get leaf-first processing order from processing_order.json
  │
Phase 3: Per-module generation
  │  For each leaf module:
  │  ├── get_prompt("system_leaf") → Get documentation writing instructions
  │  ├── read_code_components → Source written to sources/*.src, read directly
  │  └── write_doc_file → Write .md (auto Mermaid validation)
  │
  │  For each parent module:
  │  ├── Read child module .md files
  │  ├── get_prompt("overview_module") → Get overview instructions
  │  └── write_doc_file → Write overview
  │
Phase 4: get_prompt("overview_repo") → Generate repository overview overview.md
  │
Phase 5: close_session → Write metadata.json, clean up workspace, release resources
```

---

## Incremental Updates

### Problems with the Original `--update`

The original CodeWiki CLI provided a `codewiki generate --update` incremental update command, but had a bug: the CLI adapter did not pass `commit_id` when creating `DocumentationGenerator`, causing `commit_id` in `metadata.json` to always be `null`. When `_detect_changed_files()` reads `null`, it falls back to full generation. Only Web mode (`background_worker.py`) correctly writes `commit_id`, so under CLI, `--update` is effectively **always equivalent to full generation**.

This was fixed: the CLI adapter now passes `commit_id` correctly, and the MCP `close_session` tool writes `metadata.json` (with current git commit + timestamp) before cleaning up the workspace, establishing the baseline for future incremental detection.

### MCP Incremental Update Solution

Incremental detection is built into the `analyze_repo` tool with a dual-strategy approach:

```
First call to analyze_repo:
  → Generate full documentation (changes field is null)
  → close_session writes metadata.json with commit_id + timestamp

Subsequent call to analyze_repo after code changes:
  → Automatically detect changes via git diff or mtime comparison
  → Return changes field with affected_modules + cascade_modules
  → AI Agent only updates affected module documentation
```

**Change detection strategies** (by priority):

1. **Git strategy**: Read `commit_id` from `metadata.json`, run `git diff` against current HEAD, also check `git status` to capture uncommitted changes (modified + untracked files)
2. **Mtime strategy** (fallback for non-git repos): Walk source files and compare modification times against `timestamp` in `metadata.json`

> **Note**: `metadata.json` is written only when `close_session` is called. If a session ends without calling `close_session`, no baseline exists and the next `analyze_repo` silently falls back to full analysis. Always call `close_session` at the end of the workflow to ensure incremental updates work.

**Return structure**:

```json
{
  "changes": {
    "has_previous": true,
    "no_changes": false,
    "method": "git",
    "changed_files": ["auth.py"],
    "affected_modules": ["Authentication Module"],
    "cascade_modules": ["Core System", "overview"],
    "hint": "Only 1 module(s) need updating: ..."
  }
}
```

- `affected_modules`: Directly affected modules that need documentation updates
- `cascade_modules`: Indirectly affected parent modules (child docs changed, so overviews must refresh) and `overview`

### Agent Incremental Update Workflow

When `analyze_repo` returns `changes` with `no_changes: false`, the Agent executes:

```
1. Only process modules in affected_modules:
   ├── read_code_components → Read changed component source code from workspace
   └── edit_doc_file(str_replace) → Partially modify documentation (instead of full rewrite)

2. Process parent modules in cascade_modules:
   ├── Read updated child documents
   └── edit_doc_file → Refresh overview sections

3. Finally update overview.md
```

Compared to the 5-phase full generation workflow, incremental updates typically only need to process 1-3 modules, significantly reducing time.

### Implementation Details

Core code is in `codewiki/mcp/tools/analysis.py`, with 4 dedicated functions (~170 lines):

| Function | Responsibility |
|----------|----------------|
| `_detect_changes()` | Main entry point, coordinates git/mtime strategies, calls module mapping |
| `_detect_via_git()` | Git detection: commit diff + uncommitted changes (modified + untracked) |
| `_detect_via_mtime()` | Mtime fallback: walk source files, compare mtime against generation timestamp |
| `_find_affected_modules()` | Substring matching changed files → module mapping (reuses original CLI logic) |

`handle_analyze_repo()` calls `_detect_changes()` after building the component index, appending results to the `changes` field in the return JSON. On first run (no existing docs), `changes` is `null`, behavior is identical to before.

---

## Workspace Lifecycle

Each `analyze_repo` call creates a session workspace at `{repo_path}/.codewiki/sessions/{session_id}/`:

```
.codewiki/sessions/{session_id}/
├── component_index.json   # Full component index (id, type, file for each)
├── leaf_nodes.json        # Complete leaf node ID list
├── languages.json         # Language statistics
├── changes.json           # Incremental change info (optional)
├── summary.json           # Compact analysis summary
├── processing_order.json   # Leaf-first generation order (after save_module_tree)
└── sources/
    └── {sanitized_id}.src # Individual component source files
```

The workspace is automatically cleaned up when `close_session` is called. Empty parent directories are pruned as well. Sessions that expire (2-hour TTL) or are evicted (max 10 concurrent) also trigger workspace cleanup.

---

## Output Structure

The generated documentation structure is consistent with the original CodeWiki:

```
docs/
├── overview.md              # Repository overview (start reading here)
├── module1.md               # Individual module documentation
├── module2.md               # ...
├── module_tree.json         # Module hierarchy structure
├── first_module_tree.json   # Initial clustering result (immutable snapshot)
└── metadata.json            # Generation metadata (commit_id + timestamp)
```

---

## Original CLI Mode (Still Available)

If you prefer one-shot command-line generation, the original method is completely unaffected:

```bash
# Configure LLM
codewiki config set \
  --provider openai-compatible \
  --api-key YOUR_KEY \
  --base-url https://api.example.com \
  --main-model claude-sonnet-4

# One-shot generation
codewiki generate
```

See the Quick Start section in [README.md](README.md) for details.

---

## FAQ

**Q: MCP Server fails to start with missing dependencies?**
A: Make sure you have run `pip install -e .` to install CodeWiki and its dependencies. The MCP Server no longer requires CLI-specific packages like `keyring` or `click`.

**Q: analyze_repo is slow?**
A: Tree-sitter parsing for large repositories (>100K lines) takes some time, usually completing within 30 seconds. Use `include_patterns` / `exclude_patterns` to narrow the analysis scope. There are no component count or source code length truncation limits.

**Q: Mermaid validation errors?**
A: The Agent will automatically correct syntax based on validation results. If failures persist, check that `mermaid-py` is properly installed.

**Q: How to have the Agent write documentation in a specific language?**
A: Specify explicitly in the conversation: "Please generate the Wiki documentation in English." or "Please use Chinese for the documentation."

**Q: What to do when a session times out?**
A: Sessions default to a 2-hour TTL with a maximum of 10 concurrent sessions. After timeout or eviction, simply re-call `analyze_repo` to create a new session.

**Q: How to incrementally update documentation after code changes?**
A: Simply tell the AI Agent "update the Wiki documentation". When the Agent calls `analyze_repo`, it automatically detects changes and the returned `changes` field indicates which modules are affected. The Agent only updates affected module documentation instead of regenerating everything. Supports both git and non-git repository detection.

**Q: What is the granularity of incremental updates?**
A: Module-level. If any component's source file in a module changes, that module's entire documentation is marked for update. Its parent module's overview is also marked (cascading update). `overview.md` is refreshed whenever any change occurs.
