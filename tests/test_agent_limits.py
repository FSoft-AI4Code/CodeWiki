"""Agent run limits, fallback triggers, overview instructions and packaging."""

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.exceptions import UnexpectedModelBehavior

import codewiki.src.be.agent_tools.generate_sub_module_documentations as sub_mod
import codewiki.src.be.llm_services as llm_services
from codewiki.cli.models.config import Configuration
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.prompt_template import format_overview_system_prompt
from codewiki.src.config import DEFAULT_AGENT_RETRIES, DEFAULT_REQUEST_LIMIT, Config

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_round_trips_agent_limits():
    cfg = Configuration.from_dict(
        {
            "base_url": "http://x",
            "main_model": "m",
            "cluster_model": "m",
            "request_limit": 250,
            "agent_retries": 5,
        }
    )
    assert (cfg.request_limit, cfg.agent_retries) == (250, 5)
    assert Configuration.from_dict(cfg.to_dict()).request_limit == 250
    backend = cfg.to_backend_config(repo_path=".", output_dir="/tmp/out", api_key="k")
    assert (backend.request_limit, backend.agent_retries) == (250, 5)


def test_backend_config_defaults():
    cfg = Config.from_cli(
        repo_path=".",
        output_dir="/tmp/out",
        llm_base_url="http://x",
        llm_api_key="k",
        main_model="m",
        cluster_model="m",
    )
    assert cfg.request_limit == DEFAULT_REQUEST_LIMIT == 100
    assert cfg.agent_retries == DEFAULT_AGENT_RETRIES == 3


def test_fallback_covers_malformed_responses(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm_services, "create_main_model", lambda config: "main")
    monkeypatch.setattr(llm_services, "create_fallback_model", lambda config: "fallback")
    monkeypatch.setattr(
        llm_services, "FallbackModel", lambda *models, **kwargs: captured.update(kwargs)
    )
    llm_services.create_fallback_models(SimpleNamespace())
    # a 200 response with an unparseable body must also switch to the fallback model
    assert set(captured["fallback_on"]) == {llm_services.ModelAPIError, UnexpectedModelBehavior}


def test_overview_system_prompt():
    assert format_overview_system_prompt("") is None
    assert format_overview_system_prompt(None) is None
    text = format_overview_system_prompt("Write in Portuguese.")
    assert "<CUSTOM_INSTRUCTIONS>\nWrite in Portuguese.\n</CUSTOM_INSTRUCTIONS>" in text


def test_messages_put_system_first():
    assert llm_services._messages("hi", None) == [{"role": "user", "content": "hi"}]
    assert llm_services._messages("hi", "sys") == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_sub_module_agents_get_limits_and_no_literal_none(tmp_path, monkeypatch):
    seen = {}

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            seen["init"] = kwargs

        async def run(self, prompt, deps, **kwargs):
            seen["run"] = kwargs
            Path(deps.absolute_docs_path, f"{deps.current_module_name}.md").write_text("# x\n")
            return SimpleNamespace(output="ok")

    monkeypatch.setattr(sub_mod, "Agent", FakeAgent)
    monkeypatch.setattr(sub_mod, "create_fallback_models", lambda config: None)
    deps = CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components={},
        path_to_current_module=[],
        current_module_name="root",
        module_tree={},
        max_depth=2,
        current_depth=1,
        config=SimpleNamespace(max_token_per_leaf_module=4000, agent_retries=7, request_limit=321),
        custom_instructions=None,
    )
    asyncio.run(
        sub_mod.generate_sub_module_documentation(
            SimpleNamespace(deps=deps), {"billing": ["b.py::fb"]}
        )
    )
    assert seen["init"]["retries"] == 7
    assert seen["run"]["usage_limits"].request_limit == 321
    # custom_instructions=None used to be formatted into the prompt as the text "None"
    assert not seen["init"]["system_prompt"].endswith("None")


def test_every_package_is_listed_in_pyproject():
    listed = set(
        tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]
    )
    on_disk = {
        ".".join(p.parent.relative_to(ROOT).parts) for p in (ROOT / "codewiki").rglob("__init__.py")
    }
    assert on_disk - listed == set()
