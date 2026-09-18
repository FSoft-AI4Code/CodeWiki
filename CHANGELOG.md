# Changelog

All notable changes to CodeWiki. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-09-18

The first release since 1.0.1. Everything that landed on `main` in between is
listed here.

### Added

- **Artifact-aware generation** (#106). Build, CI, container, packaging,
  manifest, configuration, schema, and script files become nodes in the
  dependency graph, with heuristic edges to the code they reference. A
  deterministic artifact index goes into every module prompt, and a
  guaranteed **Build, Deployment and Configuration** module is inserted when
  clustering drops most artifact nodes. Flags: `--artifacts/--no-artifacts`,
  `--artifact-token-budget`, `--with-prose`, `--artifact-exclude`.
- **Component-level incremental updater** (#111). `--update` now diffs the
  saved dependency graph against the current code, repairs the module tree
  (renames, deletions, routing of new components, re-clustering of grown
  modules), builds a change report per leaf module, and runs one agent per
  affected module that patches its page and the pages that describe it. Falls
  back to a full build when too much changed. Every decision is written to
  `update_record.json`. Flags: `--update-rung`, `--tau-ren`, `--tau-nb`,
  `--tau-grow`, `--tau-full`, `--tau-tree`, `--k-hop`, `--max-diff-tokens`.
- `--compare-to <commit>` to set the base commit for an update (#67).
- **More complete code graphs.** Free functions become documentation units in
  function-centric projects. Scope-, namespace-, include-, and import-aware
  call resolution for C, C++, Java (#64, #68), and C# raised to the Java
  standard. An external symbol table keeps library calls out of the graph.
- **Language analyzers**: Kotlin (#41), PHP, Ruby (#97), Scala (#108).
- **Subscription providers** `claude-code` and `codex` (#60, #70): every LLM
  call goes through the local CLI via `caw`, no API key needed.
- **Atlas Cloud** provider (#69), **Azure OpenAI** (#49), **AWS Bedrock**
  (#40).
- **MCP server** (#9, #66) with eight LLM-free tools (`analyze_repo`,
  `read_code_components`, `write_doc_file`, `edit_doc_file`,
  `save_module_tree`, `get_processing_order`, `get_prompt`, `close_session`)
  plus the legacy `generate_docs` and `get_module_tree`; file side-channel
  workspace; module-level change detection; `codewiki mcp` command; a Claude
  skill under `skills/`.
- **Prompt caching** for agentic calls, `--prompt-caching/--no-prompt-caching`
  (#81).
- `.gitignore` handling, `--use-gitignore/--no-gitignore` (#80).
- Configurable `--max-tokens`, `--max-token-per-module`,
  `--max-token-per-leaf-module`, `--max-depth`; `--fallback-model`;
  `--instructions` and `codewiki config agent` defaults.
- `SECURITY.md` and `LICENSE` (#107). CI workflow: pytest and ruff on
  Python 3.12.
- `CHANGELOG.md` (this file) and the `guides/` folder.

### Changed

- `--update` runs the component-level updater. The 1.x file-level
  invalidation is still available as `--update-rung 0`.
- Artifacts are documented by default. `--no-artifacts` restores the code-only
  behaviour of 1.x.
- `generator_version` in `metadata.json` is read from the package version
  instead of a hardcoded string.
- Hand-written guides moved from the top level and `docker/` into `guides/`:
  `DEVELOPMENT.md` -> `guides/development.md`, `IDE_DRIVEN_GUIDE.md` ->
  `guides/mcp-ide-mode.md`, `docker/DOCKER_README.md` -> `guides/docker.md`.
  New: `guides/cli-reference.md`, `guides/providers.md`,
  `guides/artifact-aware-generation.md`, `guides/incremental-updates.md`.
- README rewritten around 2.0. The benchmark section reports the 2.0
  rebenchmark on the seven CodeWikiBench repositories, with DeepWiki and 1.0
  re-run under the same setting.
- Component ids reformatted to `path::name` (#48). Ruff rule set pinned;
  Node.js requirement clarified as install-time only.
- Project URLs in `pyproject.toml` point at the real repository.

### Fixed

- Overview and module prompts exceeding the Codex input cap on large
  repositories (#92, #94).
- Resume skipping modules whose documentation was missing (#94).
- `--update` in whole-repository mode not regenerating invalidated modules
  (#100).
- The CLI adapter not passing `commit_id`, which made `--update` behave like
  a full build.
- Leaf-node filtering dropping identifiers such as `handleInvalidInput`
  (#102); leaf-node reduction now logged at INFO (#103).
- C codebases producing zero leaf nodes.
- `node_modules` excluded from analysis; missing runtime dependencies added;
  non-standard responses from OpenAI-compatible proxies handled.
- Mermaid validation hang in the MCP server; MCP path-traversal guards and
  edit-history caps.
- Star history chart in the README (#95); mermaid-py behaviour comments (#110).

## [1.0.1] - 2025-10-18

- CLI release: `codewiki config set`, `codewiki config show`,
  `codewiki config validate`, `codewiki generate` with `--output`,
  `--create-branch`, `--github-pages`, `--no-cache`, `--include`,
  `--exclude`, `--focus`, `--doc-type`, `--verbose`.
- API keys stored in the system keychain with a file fallback.

## [1.0.0] - 2025-09

- Initial public release with the paper: hierarchical decomposition,
  recursive agents, Mermaid diagrams, seven languages (Python, Java,
  JavaScript, TypeScript, C, C++, C#), web application, Docker image,
  CodeWikiBench.

[2.0.0]: https://github.com/FSoft-AI4Code/CodeWiki/releases/tag/v2.0.0
