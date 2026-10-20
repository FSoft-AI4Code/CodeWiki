"""Google (Gemini API / Vertex AI) provider: config, gates and CLI diagnostics."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import codewiki.cli.commands.generate as gen
import codewiki.cli.config_manager as cm
from codewiki.cli.commands.config import config_set, config_show, config_validate
from codewiki.cli.models.config import Configuration
from codewiki.cli.utils.validation import is_top_tier_model
from codewiki.src.config import Config

MODEL = "gemini-3.8-flash"


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cm, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setenv("CODEWIKI_NO_KEYRING", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return tmp_path


def _loaded_manager():
    manager = cm.ConfigManager()
    manager.load()
    return manager


def test_vertex_configuration_valid_without_url():
    cfg = Configuration(
        provider="google-vertex",
        base_url="",
        main_model=MODEL,
        cluster_model=MODEL,
        fallback_model=MODEL,
        google_project="test-proj",
        google_location="eu",
    )
    cfg.validate()
    assert cfg.is_complete() is True

    again = Configuration.from_dict(cfg.to_dict())
    assert (again.google_location, again.google_project) == ("eu", "test-proj")


def test_vertex_is_configured_without_api_key_and_fallback_follows_main(tmp_config):
    cm.ConfigManager().save(
        provider="google-vertex",
        main_model=MODEL,
        cluster_model=MODEL,
        google_project="test-proj",
    )
    manager = _loaded_manager()
    assert manager.is_configured() is True
    assert manager.get_api_key() is None
    assert manager.get_config().fallback_model == MODEL


def test_config_set_google_reads_gemini_api_key_env(tmp_config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    result = CliRunner().invoke(
        config_set,
        ["--provider", "google", "--main-model", MODEL, "--cluster-model", MODEL],
    )
    assert result.exit_code == 0, result.output
    assert _loaded_manager().get_api_key() == "test-gemini-key"


def test_config_set_google_refuses_foreign_key(tmp_config):
    cm.ConfigManager().save(
        api_key="sk-old",
        base_url="https://api.example.com/v1",
        main_model="m",
        cluster_model="m",
        provider="openai-compatible",
    )
    result = CliRunner().invoke(config_set, ["--provider", "google", "--main-model", MODEL])
    assert result.exit_code != 0
    assert "GEMINI_API_KEY" in result.output
    assert _loaded_manager().get_config().provider == "openai-compatible"


def test_google_settings_reach_backend_config(tmp_path, monkeypatch):
    seen = {}
    cfg = SimpleNamespace(
        main_model=MODEL,
        cluster_model=MODEL,
        fallback_model=MODEL,
        base_url="",
        provider="google-vertex",
        google_project="test-proj",
        google_location="eu",
        aws_region="us-east-1",
        agent_instructions=None,
        max_tokens=1,
        max_token_per_module=1,
        max_token_per_leaf_module=1,
        max_depth=1,
        request_limit=1,
        agent_retries=1,
        use_gitignore=True,
        prompt_caching=False,
    )

    class Stop(Exception):
        pass

    class FakeManager:
        def load(self):
            return True

        def is_configured(self):
            return True

        def get_config(self):
            return cfg

        def get_api_key(self):
            return None

    def fake_generator(**kwargs):
        seen.update(kwargs["config"])
        raise Stop

    monkeypatch.setattr(gen, "ConfigManager", FakeManager)
    monkeypatch.setattr(gen, "CLIDocumentationGenerator", fake_generator)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    monkeypatch.chdir(tmp_path)

    CliRunner().invoke(gen.generate_command, ["-o", "out"])
    assert seen["google_project"] == "test-proj"
    assert seen["google_location"] == "eu"

    backend = Config.from_cli(
        repo_path=".",
        output_dir="out",
        llm_base_url="",
        llm_api_key="",
        main_model=MODEL,
        cluster_model=MODEL,
        provider="google-vertex",
        google_project="test-proj",
        google_location="eu",
    )
    assert (backend.google_project, backend.google_location) == ("test-proj", "eu")


@pytest.fixture
def access_checks(monkeypatch):
    """Record (provider, model) of every connectivity probe instead of calling Google."""
    from codewiki.src.be import google_provider

    calls = []

    def fake(provider, api_key, model, project="", location=""):
        calls.append((provider, model))

    monkeypatch.setattr(google_provider, "check_access", fake)
    return calls


def _save_vertex():
    cm.ConfigManager().save(
        provider="google-vertex",
        main_model=MODEL,
        cluster_model=MODEL,
        google_project="test-proj",
        google_location="eu",
    )


def test_validate_vertex_without_key_or_url(tmp_config, access_checks):
    _save_vertex()
    result = CliRunner().invoke(config_validate, [])
    assert result.exit_code == 0, result.output
    assert "API key not required" in result.output
    assert "Configuration is valid" in result.output
    assert access_checks == [("google-vertex", MODEL)]


def test_validate_google_does_not_use_openai_client(tmp_config, access_checks, monkeypatch):
    import openai

    def forbidden(*args, **kwargs):
        raise AssertionError("a Gemini key must not reach an OpenAI client")

    monkeypatch.setattr(openai, "OpenAI", forbidden)
    cm.ConfigManager().save(
        api_key="test-gemini-key",
        base_url="https://api.openai.com/v1",
        provider="google",
        main_model=MODEL,
        cluster_model=MODEL,
    )
    result = CliRunner().invoke(config_validate, [])
    assert result.exit_code == 0, result.output
    assert access_checks == [("google", MODEL)]


def test_validate_reports_google_access_failure(tmp_config, monkeypatch):
    from codewiki.src.be import google_provider

    def failing(*args, **kwargs):
        raise RuntimeError("404 model not found")

    monkeypatch.setattr(google_provider, "check_access", failing)
    _save_vertex()
    result = CliRunner().invoke(config_validate, [])
    assert result.exit_code != 0
    assert "API connectivity test failed" in result.output


def test_show_vertex_lists_project_and_location(tmp_config):
    import json

    _save_vertex()
    result = CliRunner().invoke(config_show, [])
    assert result.exit_code == 0, result.output
    assert "Google ADC" in result.output
    assert "test-proj" in result.output
    assert "eu" in result.output
    assert "Base URL" not in result.output

    as_json = CliRunner().invoke(config_show, ["--json"])
    assert json.loads(as_json.output)["google_project"] == "test-proj"


def test_gemini_3_is_top_tier():
    assert is_top_tier_model(MODEL) is True
