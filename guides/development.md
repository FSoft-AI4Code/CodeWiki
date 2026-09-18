# Development guide

For people who want to change or extend CodeWiki.

## Setup

```bash
git clone https://github.com/FSoft-AI4Code/CodeWiki.git
cd CodeWiki
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
codewiki --version               # 2.0.0
```

Requirements: Python 3.12+, Git, and Node.js with npm at install time.
Node.js is needed by PythonMonkey, which `mermaid-parser-py` pulls in.
Diagram validation itself calls a remote rendering service, not a local
Node process.

## Project layout

```
codewiki/
├── __init__.py                 # __version__
├── cli/                        # the `codewiki` command
│   ├── main.py                 # click group: generate, config, mcp, version
│   ├── commands/               # generate.py, config.py
│   ├── models/                 # config.py (AgentInstructions), job.py
│   ├── adapters/doc_generator.py   # CLI -> backend Config bridge
│   ├── config_manager.py       # ~/.codewiki/config.json + keychain
│   ├── git_manager.py, html_generator.py
│   └── utils/                  # fs, logging, progress, validation, repo_validator
├── mcp/                        # MCP server for IDE agents
│   ├── server.py               # tool registry (8 fine-grained + 2 legacy)
│   ├── session.py, workspace.py
│   └── tools/                  # analysis, code_reader, doc_writer, module_tree, prompt_server
├── src/
│   ├── config.py               # backend Config
│   ├── be/                     # generation pipeline
│   │   ├── dependency_analyzer/
│   │   │   ├── analyzers/      # one tree-sitter analyzer per language + artifact.py
│   │   │   ├── analysis/       # repo walk, call-graph analysis, analysis service
│   │   │   ├── models/         # Node, CallRelationship, ...
│   │   │   ├── utils/          # patterns.py (extensions, ignore lists), external_symbols.py
│   │   │   ├── ast_parser.py, dependency_graphs_builder.py
│   │   │   ├── leaf_selection.py, topo_sort.py
│   │   ├── cluster_modules.py  # hierarchical clustering, super-grouping, guaranteed artifact module
│   │   ├── documentation_generator.py   # bottom-up page generation, overview, metadata
│   │   ├── updater/            # component-level incremental updater (2.0)
│   │   ├── agent_tools/        # read_code_components, str_replace_editor, sub-module delegation
│   │   ├── backend.py          # LLMBackend abstraction + factory
│   │   ├── pydantic_ai_backend.py       # API-key providers
│   │   ├── caw_backend.py, caw_toolkit.py   # claude-code / codex subscription providers
│   │   ├── prompt_template.py, llm_services.py, module_naming.py
│   └── fe/                     # FastAPI web app (used by the Docker image)
├── templates/                  # HTML templates for the viewer and web app
└── run_web_app.py
docker/                         # Dockerfile, docker-compose.yml, env.example
guides/                         # these guides
docs/                           # CodeWiki's own generated documentation (do not hand-edit)
skills/                         # Claude skill for MCP-driven generation
tests/
```

## How a build runs

1. **Dependency analysis** (`src/be/dependency_analyzer/`). The repository is
   walked, each source file is parsed with tree-sitter by its language
   analyzer, and components (classes, functions, files) become nodes with
   `depends_on` edges. Free functions become documentation units when the
   project is function-centric. Calls into libraries are recognised by an
   external symbol table and dropped from the graph. The artifact analyzer
   then adds build, CI, container, packaging, manifest, config, schema, and
   script files as nodes, with heuristic edges to the code they reference.
2. **Leaf selection and clustering** (`leaf_selection.py`,
   `cluster_modules.py`). Leaf candidates are chosen, an LLM groups them
   into modules recursively down to `max_depth`, large top levels are
   super-grouped, and a guaranteed artifact module is inserted when
   clustering dropped most artifact nodes.
3. **Documentation generation** (`documentation_generator.py`). Leaf modules
   are written bottom-up by an agent that reads code through the agent
   tools and writes through a Mermaid-validating editor. Parent pages are
   written from child pages. The overview comes last. `metadata.json`
   records the model, version, and commit.
4. **Incremental update** (`updater/`), when `--update` is passed: diff the
   saved graph against the fresh one, repair the module tree, build change
   reports per leaf, decide between incremental and full, run one agent per
   active leaf, then a stale-name scan. Every decision goes to
   `update_record.json`.

## Backends

`LLMBackend` (`backend.py`) has two implementations:

- `PydanticAIBackend`: the API-key path for `openai-compatible`,
  `atlas-cloud`, `anthropic`, `bedrock`, `azure-openai`.
- `CawBackend`: the subscription path for `claude-code` and `codex`. It
  runs the module agent through the local CLI via the `caw` library and
  exposes CodeWiki's tools to the CLI over MCP (`caw_toolkit.py`).

## Agent instructions

`AgentInstructions` (`cli/models/config.py`) carries include and exclude
patterns, focus paths, doc type, custom instructions, and artifact
excludes. It flows CLI flags -> persistent config -> backend `Config` ->
dependency analyzer (file filtering) and prompts (custom instructions).

To add an option: add the field and its `to_dict`/`from_dict` handling in
`cli/models/config.py`; add the flag in `cli/commands/generate.py` and, if
it should persist, `cli/commands/config.py`; thread it through
`cli/adapters/doc_generator.py` into `src/config.py`; use it where it
matters (`dependency_analyzer/` for filtering, `prompt_template.py` for
prompts).

## Adding a language

Analyzers are standalone classes, one file per language. Follow
`analyzers/scala.py` (the newest one) as a template.

1. **Grammar.** Add the `tree-sitter-<lang>` package to `pyproject.toml`
   and `requirements.txt`.
2. **Analyzer.** Create `analyzers/<lang>.py` with a
   `TreeSitter<Lang>Analyzer` class that extracts components and
   relationships, plus a module-level `analyze_<lang>_file(...)` function.
3. **Dispatch.** In `analysis/call_graph_analyzer.py`, add an
   `_analyze_<lang>_file` method and an `elif language == "<lang>"` branch
   next to the others.
4. **Register the language.** Add it to the supported-language lists in
   `analysis/analysis_service.py`, the extension maps in
   `utils/patterns.py` (`EXTENSION_TO_LANGUAGE`, include patterns), the
   extension list in `ast_parser.py`, and the CLI validators
   (`cli/utils/validation.py`, `cli/utils/repo_validator.py`).
5. **Tests.** Add `tests/test_<lang>_analyzer.py` with small source
   snippets that check component and edge extraction. See
   `tests/test_scala_analyzer.py` and `tests/test_ruby_analyzer.py`.
6. **Docs.** Add the language to the README list.

## Tests and lint

```bash
pytest -q -o addopts=""                 # whole suite, fast
pytest tests/test_updater_orchestrator.py -q
pytest --cov=codewiki tests/            # with coverage
ruff check codewiki tests
ruff format --check codewiki tests
```

CI (`.github/workflows/ci.yml`) runs on pushes and pull requests to `main`
with Python 3.12: the test suite, then `ruff check` and `ruff format --check`
on the Python files the change touched. The lint rule set is pinned in
`pyproject.toml`.

The same two ruff checks run locally as a `pre-commit` hook. Enable it once
per clone:

```bash
git config core.hooksPath .githooks
```

The hook (`.githooks/pre-commit`) lints the staged content of every Python
file in the commit and aborts the commit if either check fails. It uses
`.venv/bin/ruff` when present, otherwise `ruff` on `PATH`. Fix the reported
files with `ruff check --fix` and `ruff format`, stage them, and commit again.
`git commit --no-verify` skips the hook; CI will still fail on the same errors.

Test files worth knowing: `test_artifact_analyzer.py`,
`test_updater_*.py` with the toy repository in `updater_toy.py`,
`test_cluster_partitioning.py`, `test_leaf_selection.py`,
`test_gitignore_filtering.py`, `test_prompt_caching.py`,
`smoke_test_mcp.py`.

## Releasing

1. Bump `__version__` in `codewiki/__init__.py` and `version` in
   `pyproject.toml`. `metadata.json` picks the version up from there.
2. Add a section to `CHANGELOG.md`.
3. Tag `vX.Y.Z` and create a GitHub release with the changelog section as
   its notes.

## Debugging

```bash
codewiki generate --verbose
export CODEWIKI_LOG_LEVEL=DEBUG
```

Common problems:

- **Tree-sitter parse errors**: check the file encoding (UTF-8 expected) and
  that the grammar package for that language is installed.
- **Provider errors**: `codewiki config validate` tests the connection. In
  subscription mode, make sure `claude` or `codex` is on `PATH` and logged
  in.
- **Prompt too large**: lower `--max-token-per-module` or
  `--max-token-per-leaf-module`, or narrow with `--include`/`--focus`.
- **Update did a full build**: read `fallback` in `update_record.json`. The
  change probably crossed `tau_full` or `tau_tree`.

## Contributing

1. Fork and create a branch: `git checkout -b feat/your-feature`.
2. Enable the lint hook: `git config core.hooksPath .githooks`.
3. Make the change and add or update tests.
4. Run the tests and lint above.
5. Open a pull request against `main`. Describe what changed and why.

Questions: https://github.com/FSoft-AI4Code/CodeWiki/issues
