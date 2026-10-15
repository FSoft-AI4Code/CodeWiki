"""`codewiki generate --main-model/--cluster-model/--fallback-model` override the saved config."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import codewiki.cli.commands.generate as gen


class Stop(Exception):
    pass


@pytest.fixture
def captured(tmp_path, monkeypatch):
    seen = {}
    cfg = SimpleNamespace(
        main_model="saved-main",
        cluster_model="saved-cluster",
        fallback_model="saved-fallback",
        base_url="http://x",
        provider="openai-compatible",
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

    class FakeManager:
        def load(self):
            return True

        def is_configured(self):
            return True

        def get_config(self):
            return cfg

        def get_api_key(self):
            return "k"

    def fake_generator(**kwargs):
        seen.update(kwargs["config"])
        raise Stop

    monkeypatch.setattr(gen, "ConfigManager", FakeManager)
    monkeypatch.setattr(gen, "CLIDocumentationGenerator", fake_generator)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    monkeypatch.chdir(tmp_path)
    return seen


def test_saved_models_by_default(captured):
    CliRunner().invoke(gen.generate_command, ["-o", "out"])
    assert (captured["main_model"], captured["cluster_model"], captured["fallback_model"]) == (
        "saved-main",
        "saved-cluster",
        "saved-fallback",
    )


def test_flags_override_saved_models(captured):
    CliRunner().invoke(
        gen.generate_command,
        ["-o", "out", "--main-model", "m", "--cluster-model", "c", "--fallback-model", "f"],
    )
    assert (captured["main_model"], captured["cluster_model"], captured["fallback_model"]) == (
        "m",
        "c",
        "f",
    )
