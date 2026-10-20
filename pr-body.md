- Adds `google` (Gemini API / AI Studio) and `google-vertex`
  (Vertex AI) as native providers, using the `google-genai` SDK
  directly instead of routing through the OpenAI-compatible shim
- Fixes Gemini 3.x compatibility: thought signatures are
  round-tripped correctly via pydantic-ai's GoogleModel
- Vertex AI is ~50x cheaper than Opus for doc generation ($0.05 vs
  $0.32-$22.75 per repo in a four-repo trial)

## Changes

**New module: `codewiki/src/be/google_provider.py`**
- `create_client()`: builds a `genai.Client` for either provider
  (API key for `google`, ADC for `google-vertex`)
- `create_google_model()`: wraps the client in pydantic-ai's
  `GoogleModel` via `GoogleProvider(client=...)` (works on both
  pydantic-ai 1.x and 2.x)
- `call_google()`: sync one-shot completions (callers run inside an
  active event loop, so async `model_request_sync` would deadlock)
- `check_access()`: credential validation against the model metadata
  endpoint
- Cost tracking via `genai-prices` through pydantic-ai's
  `ModelResponse.cost()`

**Config changes:**
- `codewiki/src/config.py`: `google_project`, `google_location`
  fields
- `codewiki/cli/models/config.py`: serialization, validation,
  `is_complete()` updates
- `codewiki/cli/config_manager.py`: Google-aware validation gating
  (no API key required for Vertex, no base URL for either)
- `codewiki/cli/commands/config.py`: `--provider google|google-vertex`,
  `--google-project`, `--google-location` options; `config show` and
  `config validate` branches for Google providers

**Integration:**
- `codewiki/src/be/backend.py`: `GOOGLE_PROVIDERS` set,
  `is_google_provider()`, `needs_api_key()`, `needs_base_url()`
- `codewiki/src/be/llm_services.py`: Google branch in
  `create_main_model` / `create_fallback_model` / `call_llm`
- `codewiki/src/be/pydantic_ai_backend.py`: `_run_usage_with_cost()`
  for Google agent-loop cost tracking
- `pyproject.toml`: `google-genai` dependency

**Docs:** `guides/providers.md`, `guides/cli-reference.md`,
`guides/development.md`, `README.md`, `CHANGELOG.md`

## Test results

17 new tests across two files:
- `tests/test_google_config.py` (11 tests): provider config,
  validation, serialization, `is_complete()`, `needs_api_key()`,
  `needs_base_url()`
- `tests/test_google_provider.py` (6 tests): model normalisation,
  client creation, `call_google` usage extraction, cost tracking

All 332 tests pass (existing + new). No changes to existing test
files.

Live tested on Vertex AI (`google-vertex`, `gemini-3.8-flash`,
`eu` and `global` locations) against real repositories.

## Breaking changes

None. Existing providers (openai, anthropic, bedrock) are unaffected.
The `google-genai` dependency is new but optional at runtime for
non-Google providers.

## Notes for reviewers

- `GoogleProvider(client=genai.Client(...))` is used instead of
  `GoogleProvider(vertexai=True)` because the latter only exists in
  pydantic-ai 1.x; the client form works on both 1.x and 2.x.
- Vertex AI never receives an API key in `create_client()` to
  prevent the SDK from switching to Express Mode.
- The genai client must be assigned to a variable before use (not
  created inline) to prevent Python GC from closing the underlying
  httpx client mid-request.
- `trial.sh` is included for testing convenience but can be excluded
  from the merge if preferred.
