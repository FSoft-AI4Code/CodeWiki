"""Tests for max_retries PydanticAI agent configuration."""

from unittest.mock import patch

from codewiki.cli.models.config import Configuration
from codewiki.src.config import Config

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel


def _make_config(max_retries: int | None = None) -> Config:
    """Build a minimal Config for testing."""
    kwargs = {
        "repo_path": "/tmp/repo",
        "output_dir": "/tmp/out",
        "llm_base_url": "http://localhost:1/v1",
        "llm_api_key": "test-key",
        "main_model": "test-model",
        "cluster_model": "test-model",
    }

    if max_retries is not None:
        kwargs["max_retries"] = max_retries

    return Config.from_cli(**kwargs)


def test_config_max_retries_defaults_to_three():
    """Config defaults max_retries to 3."""
    config = _make_config()
    assert config.max_retries == 3


def test_config_accepts_max_retries():
    """Config accepts a custom max_retries value."""
    config = _make_config(max_retries=5)
    assert config.max_retries == 5


def test_configuration_round_trip_preserves_max_retries():
    """Configuration serialization preserves max_retries."""
    config = Configuration(
        base_url="http://localhost:1/v1",
        main_model="test-model",
        cluster_model="test-model",
        max_retries=7,
    )

    restored = Configuration.from_dict(config.to_dict())
    assert restored.max_retries == 7


def test_agent_retries_failed_tool_call():
    """Agent retries a tool call after ModelRetry."""
    tool_attempts = 0
    model_calls = 0

    def model_function(messages, info):
        nonlocal model_calls
        model_calls += 1

        if model_calls <= 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="test_tool",
                        args={},
                    )
                ]
            )

        return ModelResponse(
            parts=[TextPart("success")],
        )

    agent = Agent(
        FunctionModel(model_function),
        retries=1,
    )

    @agent.tool_plain
    def test_tool() -> str:
        nonlocal tool_attempts
        tool_attempts += 1

        if tool_attempts == 1:
            raise ModelRetry("Intentional failure")

        return "success"

    result = agent.run_sync("Call the test tool")

    assert result.output == "success"
    assert tool_attempts == 2
    assert model_calls == 3
