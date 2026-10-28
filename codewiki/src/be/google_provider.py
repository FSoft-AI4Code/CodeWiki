"""Native Google provider: Gemini API (``google``) and Vertex AI (``google-vertex``).

Agent loops use pydantic-ai's GoogleModel, which round-trips Gemini 3 thought
signatures across tool-call turns. The genai client is built here and passed
as ``GoogleProvider(client=...)``: the one constructor shared by pydantic-ai
1.x and 2.x (2.x moved Vertex AI to ``GoogleCloudProvider``), and it keeps
``GOOGLE_API_KEY`` / ``GOOGLE_GENAI_USE_VERTEXAI`` in the environment from
switching modes.

One-shot completions use the synchronous genai client, because callers run
them from inside an active event loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google import genai
from google.genai import types as genai_types
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

# gemini-3.x models are served from the global, us and eu locations; the SDK
# default (us-central1) does not serve them.
DEFAULT_VERTEX_LOCATION = "global"
# The OpenAI SDK's default request timeout; genai sets none of its own.
_TIMEOUT_MS = 600_000
_RETRY_ATTEMPTS = 3
_MODEL_PREFIXES = ("google/", "models/", "google-gla:", "google-vertex:", "google-cloud:")


def normalise_model_name(name: str) -> str:
    """Strip routing prefixes (``google/``, ``google-gla:`` ...) the Google API rejects."""
    for prefix in _MODEL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _http_options() -> genai_types.HttpOptions:
    options: dict[str, Any] = {"timeout": _TIMEOUT_MS}
    retry_cls = getattr(genai_types, "HttpRetryOptions", None)
    if retry_cls is not None:  # absent in older google-genai releases
        options["retry_options"] = retry_cls(attempts=_RETRY_ATTEMPTS)
    return genai_types.HttpOptions(**options)


def create_client(
    provider: str, api_key: str | None, project: str = "", location: str = ""
) -> genai.Client:
    """Build a genai client for ``google`` (API key) or ``google-vertex`` (ADC)."""
    if provider == "google-vertex":
        project = project or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise ValueError(
                "google-vertex needs a Google Cloud project: pass --google-project "
                "('codewiki config set --google-project <id>') or set GOOGLE_CLOUD_PROJECT."
            )
        # Never an api_key: with one, genai switches to Vertex Express Mode, and
        # the stored key may belong to another provider.
        return genai.Client(
            vertexai=True,
            project=project,
            location=location or os.getenv("GOOGLE_CLOUD_LOCATION") or DEFAULT_VERTEX_LOCATION,
            http_options=_http_options(),
        )
    if not api_key:
        raise ValueError(
            "google needs a Gemini API key: run 'codewiki config set --api-key <key>' "
            "or set GEMINI_API_KEY before 'config set'."
        )
    return genai.Client(vertexai=False, api_key=api_key, http_options=_http_options())


def check_access(
    provider: str, api_key: str | None, model: str, project: str = "", location: str = ""
) -> None:
    """Raise if the credentials cannot see *model* (auth, project, location or name wrong)."""
    client = create_client(provider, api_key, project, location)
    client.models.get(model=normalise_model_name(model))


def _client_for(config) -> genai.Client:
    return create_client(
        config.provider,
        config.llm_api_key,
        getattr(config, "google_project", ""),
        getattr(config, "google_location", ""),
    )


# One-shot calls reuse a sync client per credential set. Agent-loop models get a
# fresh client (_client_for): their async transport is bound to the event loop.
_SYNC_CLIENTS: dict[tuple[str, str | None, str, str], genai.Client] = {}


def _sync_client_for(config) -> genai.Client:
    key = (
        config.provider,
        config.llm_api_key,
        getattr(config, "google_project", ""),
        getattr(config, "google_location", ""),
    )
    client = _SYNC_CLIENTS.get(key)
    if client is None:
        client = _SYNC_CLIENTS[key] = _client_for(config)
    return client


def create_google_model(config, model_name: str) -> GoogleModel:
    """GoogleModel for the agent loop. No temperature and no thinking config:
    the model defaults apply (Google advises temperature 1.0 for Gemini 3)."""
    return GoogleModel(
        normalise_model_name(model_name),
        provider=GoogleProvider(client=_client_for(config)),
        settings=GoogleModelSettings(max_tokens=config.max_tokens),
    )


def _usage_dict(metadata) -> dict[str, Any]:
    if metadata is None:
        return {"requests": 1}
    input_tokens = (metadata.prompt_token_count or 0) + (metadata.tool_use_prompt_token_count or 0)
    output_tokens = (metadata.candidates_token_count or 0) + (metadata.thoughts_token_count or 0)
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,  # thinking is billed as output
        "requests": 1,
    }
    if metadata.cached_content_token_count:
        usage["cache_read_tokens"] = metadata.cached_content_token_count
    return usage


def call_google(
    prompt: str, config, model: str, system_prompt: str | None = None
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """One-shot completion. Returns (text or None, usage dict).

    None mirrors ``_extract_content``: empty, truncated-before-text or blocked
    output is logged and returned as None, never raised.
    """
    name = normalise_model_name(model)
    client = _sync_client_for(config)
    response = client.models.generate_content(
        model=name,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            max_output_tokens=config.max_tokens,
        ),
    )
    usage = _usage_dict(response.usage_metadata)
    from codewiki.src.be.api_usage import normalize_api_usage, record_response
    from codewiki.src.be.pricing import price_usage

    actual_model = response.model_version or name
    priced = price_usage(
        normalize_api_usage(usage),
        getattr(config, "pricing", {}),
        config.provider,
        actual_model,
        model,
    )
    if "cost_usd" in priced:
        usage["cost_usd"] = priced["cost_usd"]
    record_response(usage, model, model=actual_model)

    candidate = response.candidates[0] if response.candidates else None
    if candidate is None:
        feedback = response.prompt_feedback
        reason = feedback.block_reason if feedback and feedback.block_reason else None
        logger.warning("Gemini returned no candidates (model=%s, block_reason=%s).", name, reason)
        return None, usage
    finish = candidate.finish_reason
    parts = candidate.content.parts if candidate.content and candidate.content.parts else []
    text = "".join(p.text for p in parts if p.text and not p.thought) or None
    if finish == genai_types.FinishReason.MAX_TOKENS:
        logger.warning(
            "Gemini response from %s stopped at max_output_tokens (finish_reason=MAX_TOKENS); "
            "output is truncated%s. Thinking tokens count against this limit.",
            name,
            " and empty" if text is None else "",
        )
    elif text is None:
        logger.warning("Gemini returned no text (model=%s, finish_reason=%s).", name, finish)
    return text, usage
