# Providers and models

CodeWiki calls an LLM for module clustering and for writing each page. You
pick where those calls go with `codewiki config set --provider ...`. Nine
providers are supported. Three of them need no API key at all.

| Provider | Needs | Good for |
| --- | --- | --- |
| `openai-compatible` (default) | API key + base URL | Any OpenAI-style endpoint: OpenAI, a LiteLLM proxy, vLLM, OpenRouter, and so on |
| `atlas-cloud` | API key | 300+ hosted models behind one OpenAI-compatible API |
| `anthropic` | API key | Direct Anthropic API |
| `azure-openai` | API key + resource URL + deployment | Azure-hosted OpenAI models |
| `bedrock` | AWS credentials + region | Anthropic and other models on AWS Bedrock |
| `google` | Gemini API key | Gemini models through the Gemini API (AI Studio) |
| `google-vertex` | Google credentials + project | Gemini models on Vertex AI, billed to a Google Cloud project, no API key |
| `claude-code` | Claude Code CLI logged in | Run on a Claude Pro or Max subscription, no per-token billing |
| `codex` | Codex CLI logged in | Run on a Codex subscription, no per-token billing |

`config set` only changes the keys you pass. When you switch provider, pass
`--main-model` and `--cluster-model` again so no old model name is left over.

## API-key providers

```bash
# OpenAI-compatible (default)
codewiki config set \
  --provider openai-compatible \
  --api-key YOUR_API_KEY \
  --base-url https://api.example.com/v1 \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4 \
  --fallback-model glm-4p5

# Anthropic
codewiki config set \
  --provider anthropic \
  --api-key YOUR_API_KEY \
  --base-url https://api.anthropic.com \
  --main-model claude-sonnet-4 \
  --cluster-model claude-sonnet-4

# Azure OpenAI
codewiki config set \
  --provider azure-openai \
  --api-key YOUR_AZURE_KEY \
  --base-url https://YOUR_RESOURCE.openai.azure.com \
  --azure-deployment YOUR_DEPLOYMENT \
  --main-model gpt-4o \
  --cluster-model gpt-4o

# AWS Bedrock (uses your AWS credentials)
codewiki config set \
  --provider bedrock \
  --aws-region us-east-1 \
  --main-model anthropic.claude-sonnet-4-v2:0 \
  --cluster-model anthropic.claude-sonnet-4-v2:0
```

### Google (Gemini API and Vertex AI)

Both Google providers talk to Gemini natively, so Gemini 3 thought signatures
survive tool-call turns. No base URL is needed.

```bash
# Vertex AI: Google credentials (gcloud auth application-default login, or
# Workload Identity Federation in CI), no API key
codewiki config set \
  --provider google-vertex \
  --google-project YOUR_PROJECT \
  --google-location eu \
  --main-model gemini-3.8-flash \
  --cluster-model gemini-3.8-flash

# Gemini API: the key is read from GEMINI_API_KEY (or GOOGLE_API_KEY) when
# --api-key is omitted
codewiki config set \
  --provider google \
  --main-model gemini-3.8-flash \
  --cluster-model gemini-3.8-flash
```

Notes:

- **Location.** `--google-location` accepts `global`, `us` or `eu`; it defaults
  to `GOOGLE_CLOUD_LOCATION`, then `global`. `eu` and `us` need google-genai
  2.x. `us-central1` does not serve Gemini 3.x.
- **Project.** `--google-project` defaults to `GOOGLE_CLOUD_PROJECT`. With
  neither set, a run fails at startup.
- **Fallback model.** It defaults to the main model, because the usual default
  (`glm-4p5`) does not exist on Google endpoints.
- **Output budget.** Thinking tokens share the `--max-tokens` budget. If pages
  come back empty with `finish_reason=MAX_TOKENS`, raise `--max-tokens` to
  65536.
- **Privacy.** Google may use prompts sent with a free-tier AI Studio key for
  product improvement. Use a paid key, or Vertex AI, for private code.
- **Cost.** Configure Gemini API rates with `codewiki config pricing set`.
  Calls without configured rates are reported as unpriced.

### Atlas Cloud

[Atlas Cloud](https://www.atlascloud.ai) is an inference platform that exposes
LLM, image, and video models behind a single OpenAI-compatible API. The base
URL is set for you (`https://api.atlascloud.ai/v1`), and the key is read from
`$ATLASCLOUD_API_KEY` when `--api-key` is omitted.

```bash
codewiki config set \
  --provider atlas-cloud \
  --main-model anthropic/claude-sonnet-4.6 \
  --cluster-model anthropic/claude-sonnet-4.6 \
  --fallback-model zai-org/GLM-4.6
```

Browse model IDs at the [models endpoint](https://api.atlascloud.ai/v1/models)
and pick a strong coding model. Their
[coding plan](https://www.atlascloud.ai/console/coding-plan) offers
budget-friendly API access.

## Subscription mode (no API key)

Subscription mode sends every LLM call through the local `claude` or `codex`
CLI binary, using the [`caw`](https://github.com/zzjas/caw) library. You pay
with your existing subscription instead of per token.

```bash
# Claude Code: install the CLI and run `claude login` first
codewiki config set \
  --provider claude-code \
  --main-model claude-sonnet-4-6 \
  --cluster-model claude-sonnet-4-6

# Codex: install the CLI and run `codex login` first
codewiki config set \
  --provider codex \
  --main-model gpt-5.4 \
  --cluster-model gpt-5.5
```

Things to know:

- **Model names are passed straight to the CLI.** Use the bare CLI name
  (`gpt-5.4`, `claude-sonnet-4-6`), not a `openai/...` or `anthropic/...`
  prefix. If you came from `openai-compatible`, re-run `config set` with both
  `--main-model` and `--cluster-model` to clear old prefixes.
- Claude Code's own `Write`, `Edit`, and `Bash` tools are disabled inside
  CodeWiki's agent loop. All documentation writes go through CodeWiki's
  editor, which validates Mermaid diagrams.
- Large repositories can push a module prompt past the CLI's input limit.
  CodeWiki trims the module tree in that case and retries.

### Cost tracking

At the end of `codewiki generate` and `codewiki generate --update`, CodeWiki
prints tokens and estimated USD cost, grouped by model and phase. The same
report is saved in `metadata.json` under `usage`; updates also attach it to
`update_record.json` and metadata's update history. Totals cover the current
invocation, including nested agents, retries with reported usage, and fallback
models. Resumed pages add no usage; unchanged updates show zero calls and cost.

For **API providers**, set your own rates in USD per million tokens. These
example numbers are illustrative; use the rates charged by your endpoint:

```bash
codewiki config pricing set YOUR_MODEL --input 1 --output 2 \
  --cache-read 0.25 --cache-write 1.25
codewiki config pricing show --json
codewiki config pricing remove YOUR_MODEL
```

Rates are stored by provider and model in `~/.codewiki/config.json`.
`--provider` defaults to the active provider. Configure main, cluster, and
fallback models separately when their names differ. Cache rates default to
the input rate when omitted, and explicit zero rates are supported. The report
records the effective rates and both requested and returned model names;
returned-model rates take precedence over the requested alias's rates.

Cached tokens are included in input totals, not added again. Costs apply each
cache rate to its subset and the input rate to the remaining input. Unknown
prices or unavailable usage never block generation: estimates are labeled
`partial` or `unknown`, with the affected models listed. Failed runs save and
print available usage with an incomplete marker. Estimates are not invoices.

**Subscription providers** retain the cost returned through their integration;
CodeWiki's API pricing settings do not override it. The displayed cost is an
API-equivalent estimate; your subscription is billed as usual.

- **Claude Code** reports the cost itself.
- **Codex** is priced from caw's rate table using the model Codex actually
  runs (`--main-model`, or `model` in `~/.codex/config.toml`). When a model
  has no rate, CodeWiki lists it as "cost unknown" instead of showing $0. Add
  a rate with:

  ```bash
  caw pricing set codex <model> --input <usd/1M> --cached <usd/1M> --output <usd/1M>
  ```

For an opt-in live check, run
`.venv/bin/python tests/smoke_test_costs.py --env-file /absolute/path/.env`
from the project root. It maps `BASE_URL`, `API_KEY`, `MAIN_MODEL`, and
`FALLBACK_MODEL` into an isolated temporary configuration, generates a tiny
repository's docs, updates them, and checks an unchanged update. Its synthetic
rates validate arithmetic rather than provider billing; logs redact credentials.

## Which model to use

- The **main model** writes the pages. It needs strong code understanding
  and long output. Claude Sonnet-class or GPT-5-class models work well.
- The **cluster model** groups components into modules. It reads large
  inputs and writes a small JSON answer. The same model as main is fine.
- The **fallback model** takes over when the main model fails a call. Pick
  something cheaper that still handles long input.

Check the setup with:

```bash
codewiki config show
codewiki config validate
```
