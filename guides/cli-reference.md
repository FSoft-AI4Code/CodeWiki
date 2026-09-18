# CLI reference

Every command and flag of the `codewiki` CLI, as of 2.0.0. For a short
introduction, start with the [README](../README.md).

Commands:

| Command | What it does |
| --- | --- |
| `codewiki generate` | Build or update the documentation for the current directory |
| `codewiki config set` | Store provider, model, and token settings |
| `codewiki config agent` | Store default include/exclude/focus/doc-type/instructions |
| `codewiki config show` | Print the stored settings (`--json` for machine-readable output) |
| `codewiki config validate` | Check the settings and test the provider connection (`--skip-api-test`, `--verbose`) |
| `codewiki mcp` | Start the MCP server for IDE agents, see [MCP / IDE-driven mode](mcp-ide-mode.md) |
| `codewiki --version` | Print the installed version |

---

## `codewiki generate`

Runs on the current working directory. There is no path argument: `cd` into
the repository first.

### Output and run control

| Flag | Default | Meaning |
| --- | --- | --- |
| `--output`, `-o PATH` | `docs` | Where the documentation is written |
| `--create-branch` | off | Create a git branch for the documentation changes |
| `--github-pages` | off | Also write `index.html`, a static viewer for GitHub Pages |
| `--no-cache` | off | Ignore cached results and rebuild everything |
| `--verbose`, `-v` | off | Show progress details and debug output |
| `--prompt-caching` / `--no-prompt-caching` | enabled | Add prompt-cache breakpoints to agent calls. Falls back to normal calls if the provider rejects them |

### What gets analyzed

| Flag | Default | Meaning |
| --- | --- | --- |
| `--include`, `-i PATTERNS` | all supported types | Comma-separated file patterns. **Replaces** the defaults completely |
| `--exclude`, `-e PATTERNS` | built-in ignore list | Comma-separated patterns. **Merged** with the built-in ignore list |
| `--focus`, `-f PATHS` | none | Comma-separated modules or paths to document in more detail |
| `--doc-type`, `-t TYPE` | none | One of `api`, `architecture`, `user-guide`, `developer` |
| `--instructions TEXT` | none | Free-form instructions passed to the documentation agent |
| `--use-gitignore` / `--no-gitignore` | enabled | Respect root and nested `.gitignore` files |

Pattern rules:

- `--include "*.cs"` analyzes only `.cs` files. Glob forms work: `*.py`,
  `src/**/*.ts`, `*.{js,jsx}`.
- `--exclude "Tests,Specs"` skips those directories **and** still skips
  `.git`, `node_modules`, `__pycache__`, `bin/`, `dist/`, and the rest of the
  built-in list. Accepts exact names (`Tests`, `.env`), globs (`*.test.js`,
  `*_test.py`), and directory patterns (`build/`, `coverage/`).
- Git ignore rules apply before the dependency analysis. Tracked files stay
  in, as in Git. Built-in and `--exclude` patterns still apply when Git
  includes a path.

### Artifact-aware generation (new in 2.0)

Build, CI, container, packaging, manifest, configuration, schema, and script
files are part of the dependency graph and get documented. Details in
[Artifact-aware generation](artifact-aware-generation.md).

| Flag | Default | Meaning |
| --- | --- | --- |
| `--artifacts` / `--no-artifacts` | enabled | Turn artifact analysis on or off. `--no-artifacts` gives the 1.x behaviour |
| `--artifact-token-budget N` | `200000` | Total token budget for artifact file contents added to the graph |
| `--with-prose` | off | Also read the root README and `docs/` as a `prose` artifact class |
| `--artifact-exclude PATTERNS` | none | Comma-separated patterns skipped by artifact analysis, e.g. `docker/data/*,config/generated/*` |

These four flags are runtime-only. `codewiki config set` and
`codewiki config agent` have no counterpart for them yet.

### Incremental updates (new in 2.0)

Refresh existing documentation after the code changed, instead of rebuilding
everything. Details in [Incremental updates](incremental-updates.md).

| Flag | Default | Meaning |
| --- | --- | --- |
| `--update` | off | Only regenerate what the changes since the last run affect |
| `--compare-to COMMIT` | stored commit | Compare against this commit instead of the one in `metadata.json`. Implies `--update`. Useful in CI and for squashed PRs |
| `--update-rung RUNG` | `3` | Updater variant: `0` = 1.x file-level invalidation, `1`, `2`, `3` = component-level updater ablation rungs, `3b` = rung 3 following 2 dependency hops |
| `--tau-ren FLOAT` | `0.95` | Body similarity above which a delete plus an add counts as a rename |
| `--tau-nb FLOAT` | `0.5` | Share of graph neighbours in one module needed to route a new component there |
| `--tau-grow FLOAT` | `0.33` | Share of new components in a module that triggers re-clustering of its parent |
| `--tau-full FLOAT` | `0.5` | Share of active modules above which a full build runs instead |
| `--tau-tree FLOAT` | `0.3` | Share of created, deleted, or re-clustered modules above which a full build runs |
| `--k-hop N` | `1` | Dependency hops followed when collecting upstream interface changes |
| `--max-diff-tokens N` | `8000` | Cap on one component diff inside a change report |

### Token limits

Override the stored limits for one run.

| Flag | Stored default | Meaning |
| --- | --- | --- |
| `--max-tokens N` | `32768` | Maximum output tokens per LLM response |
| `--max-token-per-module N` | `36369` | Input-token threshold that triggers module clustering |
| `--max-token-per-leaf-module N` | `16000` | Input-token threshold for leaf modules |
| `--max-depth N` | `2` | Maximum depth of the hierarchical decomposition |

### Examples

```bash
codewiki generate                                   # plain build into ./docs
codewiki generate --github-pages --create-branch    # with viewer, on a new branch
codewiki generate --update                          # refresh after code changes
codewiki generate --compare-to abc1234              # refresh relative to a known commit
codewiki generate --no-artifacts                    # code only, 1.x behaviour
codewiki generate --include "*.cs" --exclude "Tests,Specs,*.test.cs"
codewiki generate --focus "src/core,src/api" --doc-type architecture
codewiki generate --instructions "Focus on public APIs and include usage examples"
codewiki generate --max-tokens 16384 --max-depth 3
```

---

## `codewiki config set`

Stores provider and model settings in `~/.codewiki/config.json`. Only the
keys you pass are changed. Provider examples are in [Providers](providers.md).

| Flag | Meaning |
| --- | --- |
| `--provider NAME` | One of `openai-compatible` (default), `atlas-cloud`, `anthropic`, `bedrock`, `azure-openai`, `claude-code`, `codex` |
| `--api-key KEY` | API key. Stored in the system keychain when one is available |
| `--base-url URL` | Provider endpoint. Set automatically for `atlas-cloud` |
| `--main-model NAME` | Model for module documentation |
| `--cluster-model NAME` | Model for module clustering |
| `--fallback-model NAME` | Model used when the main model fails |
| `--aws-region REGION` | Bedrock only |
| `--api-version VERSION` | Azure OpenAI only |
| `--azure-deployment NAME` | Azure OpenAI only |
| `--max-tokens N`, `--max-token-per-module N`, `--max-token-per-leaf-module N`, `--max-depth N` | Stored token limits, see the table above |
| `--use-gitignore` / `--no-gitignore` | Stored default for Git ignore handling |
| `--prompt-caching` / `--no-prompt-caching` | Stored default for prompt caching |

Where things are stored:

- **API keys**: system keychain (macOS Keychain, Windows Credential Manager,
  Linux Secret Service). Falls back to `~/.codewiki/credentials.json` in
  headless or container environments. Set `CODEWIKI_NO_KEYRING=1` to force the
  file.
- **Settings and agent defaults**: `~/.codewiki/config.json`.

## `codewiki config agent`

Stores default analysis settings so you do not repeat them on every run.
Runtime flags on `generate` override them.

```bash
codewiki config agent --include "*.cs"
codewiki config agent --exclude "Tests,Specs,*.test.cs"
codewiki config agent --focus "src/core,src/api"
codewiki config agent --doc-type architecture
codewiki config agent --instructions "Document error handling in detail"
codewiki config agent            # show current agent defaults
codewiki config agent --clear    # remove all of them
```

## `codewiki config show` and `codewiki config validate`

```bash
codewiki config show            # human-readable
codewiki config show --json     # machine-readable
codewiki config validate        # checks settings and calls the provider once
codewiki config validate --skip-api-test --verbose
```

## `codewiki mcp`

Starts CodeWiki as an MCP server on stdio. Add it to your IDE's MCP
configuration as:

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "codewiki",
      "args": ["mcp"]
    }
  }
}
```

The server needs no LLM configuration. The IDE agent supplies the reasoning
and CodeWiki supplies the analysis tools. See
[MCP / IDE-driven mode](mcp-ide-mode.md).
