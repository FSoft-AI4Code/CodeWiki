"""
LLM service factory for creating configured LLM clients.

Includes a compatibility layer for OpenAI-compatible API proxies that may
return slightly non-standard responses (e.g. choices[].index = None).

Supports multiple providers: openai-compatible, anthropic, bedrock, azure-openai.
"""
import inspect
import logging
from typing import Optional

from openai.types import chat

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.providers.openai import OpenAIProvider
from openai import OpenAI, BadRequestError

from codewiki.src.config import Config

logger = logging.getLogger(__name__)

# (base_url, model_name) pairs whose provider rejected cache_control markers.
# Module-level because sub-agent runs re-create model instances per tool call
# (see generate_sub_module_documentations); the fallback probe should only
# happen once per provider/model, not once per sub-module.
_CACHE_UNSUPPORTED: set = set()

_EPHEMERAL_CACHE = {"type": "ephemeral"}


def _add_cache_control_to_message(message) -> None:
    """Attach an ephemeral ``cache_control`` marker to an OpenAI-format message in place.

    Tool messages get the marker at message level: OpenAI-compatible proxies
    (e.g. LiteLLM) map that onto the Anthropic ``tool_result`` block, whereas a
    part-level marker would land on a nested block, which Anthropic rejects.
    Other roles get it on the last content part (string content is converted to
    a single text part).
    """
    if message.get("role") == "tool":
        message.setdefault("cache_control", dict(_EPHEMERAL_CACHE))
        return
    content = message.get("content")
    if isinstance(content, str) and content:
        message["content"] = [
            {"type": "text", "text": content, "cache_control": dict(_EPHEMERAL_CACHE)}
        ]
    elif isinstance(content, list) and content:
        last_part = content[-1]
        if isinstance(last_part, dict):
            last_part.setdefault("cache_control", dict(_EPHEMERAL_CACHE))


def _should_use_max_completion_tokens(model_name: str, base_url: str) -> bool:
    """
    Determine whether to use max_completion_tokens instead of max_tokens.

    Newer OpenAI models (o1, o3, o4, gpt-4o, gpt-5, etc.) require
    max_completion_tokens. Anthropic and other providers still use max_tokens.
    """
    model_lower = model_name.lower()
    # OpenAI models that require max_completion_tokens
    new_openai_patterns = ("o1", "o3", "o4", "gpt-4o", "gpt-4-turbo", "gpt-5")
    if any(pattern in model_lower for pattern in new_openai_patterns):
        return True
    # If base_url points to OpenAI directly, newer models may need it
    if base_url and "api.openai.com" in base_url:
        return True
    return False


def _build_model_settings(config: Config, model_name: str) -> OpenAIChatModelSettings:
    """Build model settings with the correct token parameter.

    No temperature is sent: some providers/models (e.g. reasoning models that
    only accept temperature=1) reject explicit values, so requests rely on the
    provider default.
    """
    if _should_use_max_completion_tokens(model_name, config.llm_base_url):
        return OpenAIChatModelSettings(
            max_completion_tokens=config.max_tokens
        )
    return OpenAIChatModelSettings(
        max_tokens=config.max_tokens
    )


def _get_litellm_model_name(model_name: str, provider: str) -> str:
    """
    Get the litellm-compatible model name for a given provider.

    For Bedrock, prefixes the model name with 'bedrock/' if not already prefixed.
    For Anthropic, prefixes with 'anthropic/' if not already prefixed.
    """
    if provider == "bedrock":
        if not model_name.startswith("bedrock/"):
            return f"bedrock/{model_name}"
    elif provider == "anthropic":
        if not model_name.startswith("anthropic/"):
            return f"anthropic/{model_name}"
    return model_name


class CompatibleOpenAIModel(OpenAIChatModel):
    """OpenAIChatModel subclass that patches non-standard API proxy responses.

    Some OpenAI-compatible proxies return responses with fields like
    choices[].index set to None instead of an integer. This subclass
    fixes those fields before pydantic validation runs.
    """

    def _validate_completion(self, response: chat.ChatCompletion) -> chat.ChatCompletion:
        # Patch choices[].index: None -> sequential integer (0, 1, 2, ...)
        if response.choices:
            for i, choice in enumerate(response.choices):
                if choice.index is None:
                    choice.index = i
        return super()._validate_completion(response)


class CachingOpenAIModel(CompatibleOpenAIModel):
    """CompatibleOpenAIModel that adds Anthropic-style prompt-cache breakpoints.

    Injects ``cache_control: {"type": "ephemeral"}`` markers into the
    OpenAI-format payload at two points: the last system message (on Anthropic
    this caches the tools + system prefix) and the final message (incremental
    multi-turn caching — each turn reads the longest previously cached prefix
    and extends it). OpenAI-compatible proxies such as LiteLLM forward the
    markers to providers that support prompt caching; providers that don't
    either ignore the field or reject the request with a 4xx, in which case the
    request is retried once without markers and the (base_url, model) pair is
    remembered so later calls skip injection entirely.
    """

    # Plain class attributes (not dataclass fields) so instances copied without
    # __init__ still resolve them.
    _prompt_caching_enabled = False
    _cache_registry_key = ("", "")

    def __init__(self, model_name, *, prompt_caching=True, cache_registry_key="", **kwargs):
        super().__init__(model_name, **kwargs)
        self._prompt_caching_enabled = prompt_caching
        self._cache_registry_key = (cache_registry_key, model_name)

    @property
    def _prompt_caching_active(self) -> bool:
        return self._prompt_caching_enabled and self._cache_registry_key not in _CACHE_UNSUPPORTED

    async def _map_messages(self, messages, model_request_parameters=None, *, model_settings=None):
        base = OpenAIChatModel._map_messages
        base_params = set(inspect.signature(base).parameters)
        if "model_settings" in base_params:
            openai_messages = await super()._map_messages(
                messages, model_request_parameters, model_settings=model_settings
            )
        elif "model_request_parameters" in base_params:
            # Intermediate pydantic-ai versions accept request parameters but
            # predate the keyword-only model_settings argument.
            openai_messages = await super()._map_messages(messages, model_request_parameters)
        else:
            # Early pydantic-ai versions accept only messages.
            openai_messages = await super()._map_messages(messages)
        if self._prompt_caching_active:
            # Breakpoint 1: last system/developer message (covers tools + system).
            for message in reversed(openai_messages):
                if message.get("role") in ("system", "developer"):
                    _add_cache_control_to_message(message)
                    break
            # Breakpoint 2: final message (covers the whole conversation prefix).
            if openai_messages:
                _add_cache_control_to_message(openai_messages[-1])
        return openai_messages

    async def _completions_create(self, messages, stream, model_settings, model_request_parameters):
        if not self._prompt_caching_active:
            return await super()._completions_create(
                messages, stream, model_settings, model_request_parameters
            )
        try:
            return await super()._completions_create(
                messages, stream, model_settings, model_request_parameters
            )
        except ModelHTTPError as e:
            if e.status_code not in (400, 422):
                raise
            _CACHE_UNSUPPORTED.add(self._cache_registry_key)
            logger.warning(
                "Provider rejected prompt caching markers for model %s (HTTP %s); "
                "retrying without prompt caching.",
                self.model_name,
                e.status_code,
            )
            try:
                result = await super()._completions_create(
                    messages, stream, model_settings, model_request_parameters
                )
            except ModelHTTPError:
                # The failure wasn't (only) about caching — don't blame the markers.
                _CACHE_UNSUPPORTED.discard(self._cache_registry_key)
                raise
            logger.warning(
                "Prompt caching disabled for model %s at this endpoint; "
                "continuing with normal calls.",
                self.model_name,
            )
            return result


def _create_litellm_openai_client(config: Config) -> OpenAI:
    """
    Create an OpenAI-compatible client backed by litellm's proxy.

    litellm translates OpenAI API calls to Bedrock, Anthropic, etc.
    """
    # Configure litellm for the provider
    if config.provider == "bedrock":
        import os
        os.environ.setdefault("AWS_DEFAULT_REGION", config.aws_region)
        os.environ.setdefault("AWS_REGION_NAME", config.aws_region)

    # litellm exposes an OpenAI-compatible Router we can use,
    # but the simplest path is to use litellm.completion() directly.
    # For pydantic-ai integration, we create a proxy client.
    return OpenAI(
        api_key=config.llm_api_key or "not-needed-for-bedrock",
        base_url=config.llm_base_url or "https://api.openai.com/v1",
    )


def create_main_model(config: Config) -> CachingOpenAIModel:
    """Create the main LLM model from configuration."""
    return CachingOpenAIModel(
        model_name=config.main_model,
        prompt_caching=config.prompt_caching,
        cache_registry_key=config.llm_base_url or "",
        provider=OpenAIProvider(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key
        ),
        settings=_build_model_settings(config, config.main_model)
    )


def create_fallback_model(config: Config) -> CachingOpenAIModel:
    """Create the fallback LLM model from configuration."""
    return CachingOpenAIModel(
        model_name=config.fallback_model,
        prompt_caching=config.prompt_caching,
        cache_registry_key=config.llm_base_url or "",
        provider=OpenAIProvider(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key
        ),
        settings=_build_model_settings(config, config.fallback_model)
    )


def create_fallback_models(config: Config) -> FallbackModel:
    """Create fallback models chain from configuration."""
    main = create_main_model(config)
    fallback = create_fallback_model(config)
    return FallbackModel(main, fallback)


def create_openai_client(config: Config) -> OpenAI:
    """Create OpenAI client from configuration."""
    return OpenAI(
        base_url=config.llm_base_url,
        api_key=config.llm_api_key
    )


def _extract_content(response, model: str) -> Optional[str]:
    """Return the message content of *response*, or None if the provider gave none.

    Logs the finish_reason so output truncation (finish_reason == "length",
    which some proxies pair with ``content: null``) is visible in the logs
    instead of surfacing later as an opaque NoneType error.
    """
    choice = response.choices[0]
    content = choice.message.content
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        logger.warning(
            "LLM response from %s stopped at the max_tokens limit "
            "(finish_reason=length); output is truncated%s.",
            model,
            " and empty" if not content else "",
        )
    if content is None:
        logger.warning(
            "LLM returned no content (model=%s, finish_reason=%s); returning None.",
            model,
            finish_reason,
        )
    return content


def call_llm(
    prompt: str,
    config: Config,
    model: str = None
) -> Optional[str]:
    """
    Call LLM with the given prompt.

    Supports openai-compatible, anthropic, and bedrock providers.
    For bedrock/anthropic, uses litellm to translate the API call.

    No temperature is sent; requests rely on the provider default (some
    models reject explicit values).

    Args:
        prompt: The prompt to send
        config: Configuration containing LLM settings
        model: Model name (defaults to config.main_model)

    Returns:
        LLM response text, or None when the provider returned no content
        (e.g. output truncated at max_tokens before any text was emitted).
    """
    if model is None:
        model = config.main_model

    provider = getattr(config, "provider", "openai-compatible")

    if provider in ("bedrock", "anthropic"):
        return _call_llm_via_litellm(prompt, config, model)

    if provider == "azure-openai":
        return _call_llm_via_azure(prompt, config, model)

    # Default: OpenAI-compatible
    client = create_openai_client(config)

    # Use the correct token parameter based on model/provider; if the server
    # rejects our choice, swap to the other token kwarg and retry once.
    use_completion_tokens = _should_use_max_completion_tokens(model, config.llm_base_url)
    primary_key = "max_completion_tokens" if use_completion_tokens else "max_tokens"
    fallback_key = "max_tokens" if use_completion_tokens else "max_completion_tokens"

    base_kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        response = client.chat.completions.create(
            **base_kwargs,
            **{primary_key: config.max_tokens},
        )
    except BadRequestError as e:
        if _is_unsupported_token_param_error(e, primary_key):
            logger.info(
                "Provider rejected %s for model %s; retrying with %s.",
                primary_key, model, fallback_key,
            )
            response = client.chat.completions.create(
                **base_kwargs,
                **{fallback_key: config.max_tokens},
            )
        else:
            raise
    return _extract_content(response, model)


def _is_unsupported_token_param_error(err: BadRequestError, param: str) -> bool:
    """Return True if *err* is the OpenAI "unsupported_parameter" error for *param*."""
    body = getattr(err, "body", None) or {}
    if isinstance(body, dict):
        error = body.get("error") or {}
        if isinstance(error, dict):
            if error.get("param") == param and error.get("code") == "unsupported_parameter":
                return True
    # Fallback: message-based sniff for proxies that don't preserve structure
    msg = str(err).lower()
    return "unsupported parameter" in msg and param in msg


def _call_llm_via_litellm(
    prompt: str,
    config: Config,
    model: str
) -> Optional[str]:
    """
    Call LLM via litellm for Bedrock/Anthropic providers.

    litellm handles the provider-specific API translation automatically.
    """
    import litellm
    import os

    litellm_model = _get_litellm_model_name(model, config.provider)

    if config.provider == "bedrock":
        os.environ.setdefault("AWS_DEFAULT_REGION", config.aws_region)
        os.environ.setdefault("AWS_REGION_NAME", config.aws_region)
        logger.debug("Calling Bedrock model %s in region %s", litellm_model, config.aws_region)
    elif config.provider == "anthropic":
        logger.debug("Calling Anthropic model %s via litellm", litellm_model)

    response = litellm.completion(
        model=litellm_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=config.max_tokens,
        api_key=config.llm_api_key if config.provider != "bedrock" else None,
    )
    return _extract_content(response, litellm_model)


def _call_llm_via_azure(
    prompt: str,
    config: Config,
    model: str
) -> Optional[str]:
    """
    Call LLM via Azure OpenAI.

    Uses the AzureOpenAI client from the openai package with
    azure_endpoint, api_version, and deployment name.
    """
    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=config.llm_api_key,
        api_version=config.api_version,
        azure_endpoint=config.llm_base_url,
    )

    deployment = config.azure_deployment or model
    logger.debug("Calling Azure OpenAI deployment %s (api_version=%s)", deployment, config.api_version)

    response = client.chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=config.max_tokens,
    )
    return _extract_content(response, deployment)
