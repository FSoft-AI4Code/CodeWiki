<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./img/black-background-logo.png">
    <img src="./img/white-background-logo.png" alt="CodeWiki" width="560">
  </picture>
</p>

<p align="center">
  <strong>Repository-level documentation for large codebases, written by AI agents from the dependency graph.</strong>
</p>

<p align="center">
  <a href="https://python.org/"><img alt="Python version" src="https://img.shields.io/badge/python-3.12+-blue?style=flat-square" /></a>
  <img alt="Version" src="https://img.shields.io/badge/version-2.0.0-blue?style=flat-square" />
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" /></a>
  <a href="https://github.com/FSoft-AI4Code/CodeWiki/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/FSoft-AI4Code/CodeWiki/ci.yml?branch=main&style=flat-square&label=CI" /></a>
  <a href="https://aclanthology.org/2026.findings-acl.288/"><img alt="Paper: Findings of ACL 2026" src="https://img.shields.io/badge/paper-Findings%20of%20ACL%202026-b31b1b?style=flat-square" /></a>
  <a href="https://github.com/FSoft-AI4Code/CodeWiki/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/FSoft-AI4Code/CodeWiki?style=flat-square" /></a>
</p>

<p align="center">
  <a href="#quick-start"><strong>Quick start</strong></a> •
  <a href="#whats-new-in-20"><strong>What's new in 2.0</strong></a> •
  <a href="#features"><strong>Features</strong></a> •
  <a href="#benchmark-results"><strong>Benchmark</strong></a> •
  <a href="./guides/README.md"><strong>Guides</strong></a> •
  <a href="https://aclanthology.org/2026.findings-acl.288/"><strong>Paper</strong></a>
</p>

<p align="center">
  📚 <strong>CodeWiki documents itself.</strong> Browse the documentation it generated for this repository at
  <a href="https://fsoft-ai4code.github.io/CodeWiki/docs/index.html">CodeWiki docs</a>.
</p>

<p align="center">
  <img src="./img/framework-overview.png" alt="CodeWiki framework" width="600" style="border: 2px solid #e1e4e8; border-radius: 12px; padding: 20px;"/>
</p>

---

CodeWiki reads a repository, builds its dependency graph, splits the graph into
a hierarchy of modules, and has agents write one page per module plus an
overview, with Mermaid diagrams. It works on codebases from a few thousand to
over a million lines, in 11 languages. It needs an LLM API key, or a Claude or
Codex subscription, or an AI IDE that speaks MCP.

## Quick start

**1. Install**

```bash
pip install git+https://github.com/FSoft-AI4Code/CodeWiki.git
codewiki --version
```

Needs Python 3.12+, Git, and Node.js with npm at install time.

**2. Pick a provider**

```bash
# Any OpenAI-compatible endpoint (OpenAI, LiteLLM proxy, OpenRouter, ...)
codewiki config set \
  --provider openai-compatible \
  --api-key YOUR_API_KEY \
  --base-url https://api.example.com/v1 \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4

# Or: no API key, run on your Claude Code subscription (`claude login` first)
codewiki config set --provider claude-code \
  --main-model claude-sonnet-4-6 --cluster-model claude-sonnet-4-6
```

Anthropic, Azure OpenAI, AWS Bedrock, Atlas Cloud, and Codex are also
supported. See [Providers and models](./guides/providers.md).

**3. Generate**

```bash
cd /path/to/your/project
codewiki generate                     # writes ./docs/
codewiki generate --github-pages      # also writes an HTML viewer
codewiki generate --update            # later: refresh after code changes
```

![CLI usage example](https://github.com/FSoft-AI4Code/CodeWiki/releases/download/assets/cli-usage-example.gif)

## What's new in 2.0

The documentation an agent can write is bounded by what the dependency graph
contains. 2.0 makes that graph more complete, wider, and keeps it current.
Full list in the [CHANGELOG](./CHANGELOG.md).

- **More complete dependency graphs.** Free functions become documentation
  units, so C code and function-centric C++, Java, and C# packages are no
  longer nearly empty. Call resolution in C, C++, Java, and C# is scope-,
  namespace-, include-, and import-aware. Library calls are filtered by an
  external symbol table. Ruby and Scala analyzers were added.
- **Artifact-aware generation.** Build files, CI workflows, Dockerfiles,
  package manifests, packaging scripts, configuration, and schemas become
  graph nodes with edges to the code they reference. They are clustered and
  documented like code, with a guaranteed **Build, Deployment and
  Configuration** module when clustering drops them.
  [Guide](./guides/artifact-aware-generation.md)
- **Component-level incremental updates.** `codewiki generate --update`
  diffs the saved graph against the current code, repairs the module tree,
  and sends one agent per affected module to patch its page and the pages
  that describe it. Everything else is left untouched. On svelte, one
  update after one commit cost $0.34 against $21.48 for a full build.
  [Guide](./guides/incremental-updates.md)
- **Subscription mode.** Run on a Claude Pro/Max or Codex subscription
  through the `claude` and `codex` CLIs, with no API key.
- **MCP server for IDE agents.** `codewiki mcp` exposes the analysis
  toolchain to Cursor, Claude Desktop, Claude Code, or CodeBuddy. The IDE's
  own model does the writing. [Guide](./guides/mcp-ide-mode.md)
- Also: Atlas Cloud provider, prompt caching, `.gitignore` handling, Kotlin
  and PHP analyzers, a CI workflow, a security policy.

## Features

**Languages.** Python, Java, JavaScript, TypeScript, C, C++, C#, Kotlin,
PHP, Ruby, Scala.

**Hierarchical decomposition.** The dependency graph is clustered into a
module tree, recursively, so a 1.4M-line repository gets the same treatment
as a 10k-line one. Depth and size thresholds are configurable.

**Recursive agents.** One agent per leaf module reads the code through tools
and writes the page. Parent pages are written from child pages. Diagrams are
validated before they are saved.

**Artifacts are documented.** On by default. `--no-artifacts` gives the 1.x
behaviour, `--with-prose` also reads README and `docs/`,
`--artifact-exclude` skips generated configuration trees.

**Incremental updates.** `--update` refreshes only what changed.
`--compare-to <commit>` sets the base commit for CI and squashed merges. If
too much changed, it falls back to a full build and says so.

**Customization.**

```bash
codewiki generate --include "*.cs" --exclude "Tests,Specs,*.test.cs"
codewiki generate --focus "src/core,src/api" --doc-type architecture
codewiki generate --instructions "Focus on public APIs and include usage examples"
codewiki config agent --exclude "Tests,Specs"      # make it the default
```

`--include` replaces the default file set. `--exclude` merges with the built-in
ignore list. Every flag is in the [CLI reference](./guides/cli-reference.md).

**IDE-driven mode.** Add `{"command": "codewiki", "args": ["mcp"]}` to your
IDE's MCP servers and ask the agent to document the repository. No LLM
configuration in CodeWiki.

## Output

```
./docs/
├── overview.md                  # start here
├── <module>.md ...              # one page per module, leaves and parents
├── module_tree.json             # the module hierarchy
├── first_module_tree.json       # clustering result before super-grouping
├── metadata.json                # model, version, commit, statistics
├── update_record.json           # every decision of the last --update run
├── temp/artifact_index.json     # artifact files by class
├── temp/dependency_graphs/      # the saved graph (used by --update)
└── index.html                   # viewer (with --github-pages)
```

This repository's own output is checked in under [`./docs/`](./docs/).

## Benchmark results

Evaluated on [CodeWikiBench](https://github.com/FSoft-AI4Code/CodeWikiBench):
seven repositories, 486 rubric requirements, the same LLM judge for every
system. Scores are weighted rubric scores from 0 to 100. DeepWiki and
CodeWiki 1.0 were re-run under the same setting as 2.0 (September 2026), so
the numbers differ from the ones in the paper.

| Repository | Language | LoC | DeepWiki | CodeWiki 1.0 | CodeWiki 2.0 | 2.0 vs 1.0 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| OpenHands | Python | 229,909 | 74.12 | 83.31 | **83.53** | +0.22 |
| svelte | JavaScript | 124,576 | 69.83 | 73.21 | **80.09** | +6.88 |
| puppeteer | TypeScript | 136,302 | 65.57 | 84.16 | **85.04** | +0.88 |
| ml-agents | C# | 86,106 | 75.74 | 80.92 | **89.35** | +8.43 |
| logstash | Java | 117,485 | 56.31 | 59.42 | **77.85** | +18.43 |
| Wazuh | C | 1,446,730 | 69.91 | 65.38 | **88.14** | +22.76 |
| Electron | C++ | 184,234 | 43.92 | 42.12 | **71.59** | +29.47 |
| **Average** | | | 65.06 | 69.79 | **82.23** | **+12.44** |

The gain is largest where the 1.0 graph missed the most structure (C, C++,
Java, C#) and near zero where the analyzers did not change (Python,
JavaScript, TypeScript). Artifacts lift the build and testing topics, which
1.0 could not see at all.

| Topic | Requirements | DeepWiki | CodeWiki 1.0 | CodeWiki 2.0 |
| --- | ---: | ---: | ---: | ---: |
| Architecture | 128 | 72.95 | 78.02 | **82.00** |
| Functionality | 211 | 61.62 | 70.46 | **88.65** |
| Operations | 63 | 67.71 | **78.24** | 78.10 |
| Build and deployment | 27 | 62.84 | 28.16 | **71.90** |
| Security | 29 | 48.73 | 64.91 | **83.20** |
| Testing | 12 | 61.43 | 41.93 | **63.25** |
| Performance | 16 | 43.33 | 49.06 | **70.90** |

<p align="center">
  <img src="./img/benchmark-2.0.png" alt="Graph growth and score by stage" width="900"/>
</p>

<p align="center"><em>
(a) how much the 2.0 code graph grew over 1.0; (b) nodes and edges added by artifacts;
(c) score by stage. "Conference version" is CodeWiki 1.0, "C1 + C2" is CodeWiki 2.0.
</em></p>


## Requirements

- Python 3.12+
- Node.js and npm at install time (a dependency builds against them)
- Git
- One of: an LLM API key, a Claude Code or Codex subscription, or an
  MCP-capable AI IDE

## Guides

| | |
| --- | --- |
| [CLI reference](./guides/cli-reference.md) | every command and flag |
| [Providers and models](./guides/providers.md) | API keys, Atlas Cloud, Azure, Bedrock, subscription mode |
| [Artifact-aware generation](./guides/artifact-aware-generation.md) | what gets documented beyond code, and how to tune it |
| [Incremental updates](./guides/incremental-updates.md) | how `--update` works, thresholds, the update record |
| [MCP / IDE-driven mode](./guides/mcp-ide-mode.md) | Cursor, Claude Desktop, Claude Code, CodeBuddy |
| [Development guide](./guides/development.md) | layout, pipeline, adding a language, tests, releasing |
| [Docker setup](./guides/docker.md) | the web application in a container |
| [CodeWikiBench](https://github.com/FSoft-AI4Code/CodeWikiBench) | the benchmark |
| [Live demo](https://fsoft-ai4code.github.io/codewiki-demo/) | generated documentation examples |

## Citation

CodeWiki was introduced in *CodeWiki: Evaluating AI's Ability to Generate
Holistic Documentation for Large-Scale Codebases*, published in
[Findings of ACL 2026](https://aclanthology.org/2026.findings-acl.288/).
If you use CodeWiki in your research, please cite:

```bibtex
@inproceedings{hoang-etal-2026-codewiki,
    title = "{C}ode{W}iki: Evaluating {AI}{'}s Ability to Generate Holistic Documentation for Large-Scale Codebases",
    author = "Hoang, Anh Nguyen and Le-Anh, Minh and Le, Bach and Bui, Nghi D. Q.",
    booktitle = "Findings of the {A}ssociation for {C}omputational {L}inguistics: {ACL} 2026",
    month = jul,
    year = "2026",
    address = "San Diego, California, United States",
    publisher = "Association for Computational Linguistics",
    url = "https://aclanthology.org/2026.findings-acl.288/",
    doi = "10.18653/v1/2026.findings-acl.288",
    pages = "5812--5827",
}
```

## Star history

<p align="center">
  <a href="https://star-history.dera.page/#FSoft-AI4Code/CodeWiki&type=Date">
   <picture>
     <source media="(prefers-color-scheme: dark)" srcset="https://star-history.dera.page/svg?repos=FSoft-AI4Code/CodeWiki&type=Date&theme=dark" />
     <source media="(prefers-color-scheme: light)" srcset="https://star-history.dera.page/svg?repos=FSoft-AI4Code/CodeWiki&type=Date" />
     <img alt="Star History Chart" src="https://star-history.dera.page/svg?repos=FSoft-AI4Code/CodeWiki&type=Date" />
   </picture>
  </a>
</p>

## Sponsors

CodeWiki is proudly sponsored by **FPT Software**.

<p align="center">
  <a href="https://fptsoftware.com/en">
    <img src="./img/fpt-logo.svg" alt="FPT Software" width="220" />
  </a>
</p>

## License

MIT. See [LICENSE](./LICENSE).
