"""Tests for prompt-cache breakpoint injection and its unsupported-provider fallback."""

import asyncio

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

import codewiki.src.be.llm_services as llm_services
from codewiki.src.be.llm_services import CachingOpenAIModel, _CACHE_UNSUPPORTED


def _make_model(prompt_caching: bool = True) -> CachingOpenAIModel:
    return CachingOpenAIModel(
        model_name="test-model",
        prompt_caching=prompt_caching,
        cache_registry_key="http://test-endpoint",
        provider=OpenAIProvider(base_url="http://localhost:1/v1", api_key="test-key"),
    )


def _history() -> list:
    return [
        ModelRequest(
            parts=[
                SystemPromptPart(content="You are a documentation agent."),
                UserPromptPart(content="Document module X."),
            ]
        ),
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_code_components", args="{}", tool_call_id="call_1")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="read_code_components",
                    content="def foo(): ...",
                    tool_call_id="call_1",
                )
            ]
        ),
    ]


def _map(model: CachingOpenAIModel) -> list:
    return asyncio.run(
        model._map_messages(_history(), ModelRequestParameters())
    )


def test_injects_breakpoints_on_system_and_final_message():
    _CACHE_UNSUPPORTED.clear()
    messages = _map(_make_model())

    system = messages[0]
    assert system["role"] == "system"
    assert isinstance(system["content"], list)
    assert system["content"][-1]["cache_control"] == {"type": "ephemeral"}

    final = messages[-1]
    assert final["role"] == "tool"
    # Tool messages carry the marker at message level (LiteLLM maps it onto the
    # Anthropic tool_result block).
    assert final["cache_control"] == {"type": "ephemeral"}

    # No markers anywhere else.
    for message in messages[1:-1]:
        assert "cache_control" not in message
        content = message.get("content")
        if isinstance(content, list):
            assert all("cache_control" not in part for part in content)


def test_final_user_message_gets_part_level_breakpoint():
    _CACHE_UNSUPPORTED.clear()
    model = _make_model()
    messages = asyncio.run(
        model._map_messages(
            [ModelRequest(parts=[SystemPromptPart(content="sys"), UserPromptPart(content="hi")])],
            ModelRequestParameters(),
        )
    )
    final = messages[-1]
    assert final["role"] == "user"
    assert isinstance(final["content"], list)
    assert final["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_no_injection_when_disabled():
    _CACHE_UNSUPPORTED.clear()
    messages = _map(_make_model(prompt_caching=False))
    for message in messages:
        assert "cache_control" not in message
        content = message.get("content")
        if isinstance(content, list):
            assert all("cache_control" not in part for part in content)
        else:
            assert isinstance(content, (str, type(None)))


def test_no_injection_when_registered_unsupported():
    _CACHE_UNSUPPORTED.clear()
    _CACHE_UNSUPPORTED.add(("http://test-endpoint", "test-model"))
    try:
        messages = _map(_make_model())
        for message in messages:
            assert "cache_control" not in message
    finally:
        _CACHE_UNSUPPORTED.clear()


def test_400_disables_caching_and_retries_without_markers():
    _CACHE_UNSUPPORTED.clear()
    model = _make_model()
    calls = []

    async def fake_create(self, messages, stream, model_settings, model_request_parameters):
        calls.append(self._prompt_caching_active)
        if self._prompt_caching_active:
            raise ModelHTTPError(status_code=400, model_name=self.model_name, body="bad cache_control")
        return "ok"

    original = OpenAIChatModel._completions_create
    OpenAIChatModel._completions_create = fake_create
    try:
        result = asyncio.run(
            model._completions_create(_history(), False, {}, ModelRequestParameters())
        )
        assert result == "ok"
        assert calls == [True, False]
        assert ("http://test-endpoint", "test-model") in _CACHE_UNSUPPORTED

        # Subsequent calls skip injection entirely: single uncached call.
        calls.clear()
        result = asyncio.run(
            model._completions_create(_history(), False, {}, ModelRequestParameters())
        )
        assert result == "ok"
        assert calls == [False]
    finally:
        OpenAIChatModel._completions_create = original
        _CACHE_UNSUPPORTED.clear()


def test_unrelated_400_is_not_blamed_on_caching():
    _CACHE_UNSUPPORTED.clear()
    model = _make_model()

    async def always_fails(self, messages, stream, model_settings, model_request_parameters):
        raise ModelHTTPError(status_code=400, model_name=self.model_name, body="context too long")

    original = OpenAIChatModel._completions_create
    OpenAIChatModel._completions_create = always_fails
    try:
        raised = False
        try:
            asyncio.run(model._completions_create(_history(), False, {}, ModelRequestParameters()))
        except ModelHTTPError:
            raised = True
        assert raised
        # The retry also failed, so the model must not stay flagged as cache-unsupported.
        assert ("http://test-endpoint", "test-model") not in _CACHE_UNSUPPORTED
    finally:
        OpenAIChatModel._completions_create = original
        _CACHE_UNSUPPORTED.clear()


def test_non_400_errors_propagate_without_fallback():
    _CACHE_UNSUPPORTED.clear()
    model = _make_model()
    calls = []

    async def rate_limited(self, messages, stream, model_settings, model_request_parameters):
        calls.append(1)
        raise ModelHTTPError(status_code=429, model_name=self.model_name, body="rate limited")

    original = OpenAIChatModel._completions_create
    OpenAIChatModel._completions_create = rate_limited
    try:
        raised = False
        try:
            asyncio.run(model._completions_create(_history(), False, {}, ModelRequestParameters()))
        except ModelHTTPError:
            raised = True
        assert raised
        assert calls == [1]
        assert not _CACHE_UNSUPPORTED
    finally:
        OpenAIChatModel._completions_create = original
        _CACHE_UNSUPPORTED.clear()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All prompt caching tests passed.")
