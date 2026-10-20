As a user with Google Cloud credentials or a Gemini API key, I want
to generate documentation with Gemini models natively, so that I
don't need to route through the OpenAI-compatible shim (which breaks
on Gemini 3.x thought signatures and tool-call turns).

## Problem

CodeWiki supports OpenAI, Anthropic and Bedrock as providers but has
no native Google path. Running Gemini through the OpenAI shim
(`generativelanguage.googleapis.com/v1beta/openai/`) fails on
Gemini 3.x models because the shim does not round-trip thought
signatures on tool-call turns (see #20). Vertex AI users have no
path at all.

## Why it matters

Gemini 3.8 Flash on Vertex AI is roughly 50x cheaper than Opus for
documentation generation. A four-repo trial on real repositories
showed $0.05-0.06 per repo versus $0.32-$22.75 with Opus, with
comparable output quality. For teams running CodeWiki across many
repos, the cost difference makes the pipeline viable as a scheduled
job instead of a manual one-off.

## How to see it

Not a bug -- feature request. Current workaround is using the
OpenAI-compatible endpoint, which fails on Gemini 3.x as described
in #20.

## Definition of done

- `codewiki config set --provider google --api-key <key>` configures
  the Gemini API (AI Studio) path
- `codewiki config set --provider google-vertex --google-project <id>
  --google-location <loc>` configures the Vertex AI path (ADC, no
  API key)
- `codewiki config validate` verifies credentials against the model
- `codewiki generate` produces documentation using the native
  google-genai SDK for both one-shot completions and agent loops
- Cost is tracked in metadata.json via genai-prices
- Gemini 3.x thought signatures are handled correctly (pydantic-ai's
  GoogleModel does this)
- Existing providers (openai, anthropic, bedrock) are unaffected
- Tests pass

## Context

Related: #20 (Gemini 3 via OpenAI shim).

The implementation uses `google-genai` (the unified SDK) directly:
- Agent loops go through pydantic-ai's `GoogleModel` with
  `GoogleProvider(client=genai.Client(...))`, which handles thought
  signatures
- One-shot completions use the sync genai client (callers run inside
  an active event loop, so `model_request_sync` would deadlock)
- Vertex AI authenticates via ADC, never receives an API key
  (prevents Express Mode switch)

## Solution direction

A working implementation exists at
`albertsikkema-elix/CodeWiki@feat/native-google-gemini-api-vertex-ai`
(+1080 / -48 lines, 17 new tests). PR to follow.

## Open questions

None.
