"""Native Google provider: model factories, one-shot calls, usage and cost."""

import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.genai import types as gt
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.usage import RequestUsage

from codewiki.src.be import google_provider
from codewiki.src.be.llm_services import (
    call_llm,
    create_fallback_models,
    create_main_model,
    pop_last_usage,
)
from codewiki.src.config import Config

MODEL = "gemini-3.8-flash"


def _config(**overrides) -> Config:
    kwargs = dict(
        repo_path=".",
        output_dir="out",
        llm_base_url="",
        llm_api_key="sk-1234",
        main_model=MODEL,
        cluster_model=MODEL,
        fallback_model=MODEL,
        provider="google-vertex",
        google_project="test-proj",
        google_location="eu",
    )
    kwargs.update(overrides)
    return Config.from_cli(**kwargs)


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client_spy(monkeypatch):
    calls = []
    real = google_provider.genai.Client

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(google_provider.genai, "Client", spy)
    return calls


def test_vertex_client_gets_no_api_key(clean_env, client_spy, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-env-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")

    model = create_main_model(_config())

    assert len(client_spy) == 1
    kwargs = client_spy[0]
    assert kwargs["vertexai"] is True
    assert kwargs["project"] == "test-proj"
    assert kwargs["location"] == "eu"
    assert "api_key" not in kwargs
    assert isinstance(model, GoogleModel)
    assert model.client.vertexai is True


def test_vertex_location_defaults_to_global_and_project_is_required(clean_env, client_spy):
    create_main_model(_config(google_location=""))
    assert client_spy[-1]["location"] == "global"

    with pytest.raises(ValueError) as err:
        create_main_model(_config(google_project=""))
    assert "--google-project" in str(err.value)
    assert "GOOGLE_CLOUD_PROJECT" in str(err.value)


def test_google_fallback_models_are_google_models(clean_env):
    config = _config(
        provider="google",
        llm_api_key="test-key",
        main_model="google/gemini-3.8-flash",
        fallback_model="gemini-3.8-flash",
    )
    chain = create_fallback_models(config)

    assert isinstance(chain, FallbackModel)
    assert all(isinstance(m, GoogleModel) for m in chain.models)
    assert chain.models[0].model_name == "gemini-3.8-flash"


def _response(candidates, **kwargs) -> gt.GenerateContentResponse:
    return gt.GenerateContentResponse(
        candidates=candidates,
        usage_metadata=gt.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            cached_content_token_count=40,
            candidates_token_count=20,
            thoughts_token_count=30,
        ),
        model_version=MODEL,
        **kwargs,
    )


def _text_candidate(text: str, reason=gt.FinishReason.STOP) -> gt.Candidate:
    return gt.Candidate(
        content=gt.Content(role="model", parts=[gt.Part(text=text)]), finish_reason=reason
    )


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture
def fake_models(monkeypatch):
    def install(response):
        models = FakeModels(response)
        monkeypatch.setattr(
            google_provider, "_client_for", lambda config: SimpleNamespace(models=models)
        )
        return models

    return install


def test_call_llm_google_text_and_usage(fake_models, monkeypatch):
    models = fake_models(_response([_text_candidate("hello")]))
    monkeypatch.setattr(google_provider, "_price", lambda usage, model: 0.25)
    pop_last_usage()

    text = call_llm("p", _config(provider="google", llm_api_key="test-key"), system_prompt="s")

    assert text == "hello"
    call = models.calls[0]
    assert call["model"] == MODEL
    assert call["config"].system_instruction == "s"
    assert pop_last_usage() == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 40,
        "requests": 1,
        "cost_usd": 0.25,
    }


@pytest.mark.parametrize(
    "response, reason",
    [
        (
            _response([gt.Candidate(content=None, finish_reason=gt.FinishReason.MAX_TOKENS)]),
            "MAX_TOKENS",
        ),
        (
            gt.GenerateContentResponse(
                candidates=None,
                prompt_feedback=gt.GenerateContentResponsePromptFeedback(
                    block_reason=gt.BlockedReason.SAFETY
                ),
            ),
            "SAFETY",
        ),
    ],
    ids=["max-tokens", "blocked"],
)
def test_call_llm_google_empty_returns_none(fake_models, caplog, response, reason):
    fake_models(response)
    pop_last_usage()

    with caplog.at_level(logging.WARNING):
        text = call_llm("p", _config(provider="google", llm_api_key="test-key"))

    assert text is None
    assert any(r.levelno == logging.WARNING and reason in r.getMessage() for r in caplog.records)
    if reason == "MAX_TOKENS":
        assert pop_last_usage()["output_tokens"] == 50


def test_cost_unknown_model_is_omitted(fake_models, monkeypatch):
    response = _response([_text_candidate("hello")])
    response.model_version = "not-a-real-model"
    fake_models(response)
    pop_last_usage()

    assert call_llm("p", _config(provider="google", llm_api_key="test-key")) == "hello"
    assert "cost_usd" not in pop_last_usage()

    priced = ModelResponse(parts=[], usage=RequestUsage(), model_name=MODEL)
    monkeypatch.setattr(
        ModelResponse, "cost", lambda self: SimpleNamespace(total_price=Decimal("0.5"))
    )
    messages = [priced, ModelRequest(parts=[UserPromptPart("x")]), priced]
    assert google_provider.messages_cost(messages) == 1.0
