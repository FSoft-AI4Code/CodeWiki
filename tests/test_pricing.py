"""Pricing configuration, request accounting, and persisted run reports."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

import codewiki.cli.config_manager as cm
from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
from codewiki.cli.commands.config import config_group
from codewiki.cli.models.config import Configuration
from codewiki.cli.models.job import JobStatistics
from codewiki.cli.utils.errors import APIError
from codewiki.cli.utils.instructions import _echo_usage
from codewiki.src.be.api_usage import UsageModel, normalize_api_usage, track_usage, usage_target
from codewiki.src.be.backend import UsageLedger
from codewiki.src.be.pricing import price_usage, validate_pricing
from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend
from codewiki.src.be.updater.record import UpdateRecord
from codewiki.src.config import Config


RATES = {"input": 1, "output": 2, "cache_read": 0.25, "cache_write": 1.5}
PRICING = {"openai-compatible": {"main": RATES}}


@pytest.fixture
def config_store(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cm, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setenv("CODEWIKI_NO_KEYRING", "1")
    return tmp_path


def config(tmp_path, **kwargs):
    settings = dict(
        repo_path=str(tmp_path),
        output_dir=str(tmp_path / "docs"),
        llm_base_url="https://example.invalid/v1",
        llm_api_key="test-key",
        main_model="main",
        cluster_model="cluster",
        fallback_model="backup",
        pricing=PRICING,
    )
    settings.update(kwargs)
    return Config.from_cli(**settings)


def response(name="main", input_tokens=100, output_tokens=20, cache_read_tokens=40):
    return ModelResponse(
        parts=[TextPart("ok")],
        model_name=name,
        usage=RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        ),
    )


def test_pricing_commands_preserve_other_settings(config_store):
    manager = cm.ConfigManager()
    manager.save(
        provider="openai-compatible",
        base_url="https://example.invalid/v1",
        main_model="main",
        cluster_model="main",
        fallback_model="main",
    )
    runner = CliRunner()
    result = runner.invoke(
        config_group,
        ["pricing", "set", "main", "--input", "1", "--output", "2", "--cache-read", "0.25"],
    )
    assert result.exit_code == 0, result.output
    manager = cm.ConfigManager()
    manager.load()
    cfg = manager.get_config()
    assert cfg.main_model == "main"
    assert cfg.pricing["openai-compatible"]["main"]["cache_read"] == 0.25
    manager.save(max_tokens=1234)
    again = cm.ConfigManager()
    again.load()
    assert again.get_config().pricing == cfg.pricing
    shown = runner.invoke(config_group, ["pricing", "show", "--json"])
    assert json.loads(shown.output) == cfg.pricing
    shown = runner.invoke(config_group, ["show", "--json"])
    assert json.loads(shown.output)["pricing"] == cfg.pricing
    removed = runner.invoke(config_group, ["pricing", "remove", "main"])
    assert removed.exit_code == 0, removed.output
    assert json.loads(runner.invoke(config_group, ["pricing", "show", "--json"]).output) == {}


@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "-inf"])
def test_invalid_cli_prices_do_not_save(config_store, bad):
    result = CliRunner().invoke(
        config_group, ["pricing", "set", "main", "--input", bad, "--output", "1"]
    )
    assert result.exit_code != 0
    assert not cm.CONFIG_FILE.exists()


def test_pricing_before_provider_setup_and_provider_isolation(config_store):
    runner = CliRunner()
    result = runner.invoke(
        config_group,
        ["pricing", "set", "main", "--input", "0", "--output", "0", "--provider", "google"],
    )
    assert result.exit_code == 0, result.output
    manager = cm.ConfigManager()
    manager.load()
    assert manager.get_config().pricing == {"google": {"main": {"input": 0.0, "output": 0.0}}}
    result = runner.invoke(
        config_group,
        ["pricing", "set", "main", "--input", "1", "--output", "1", "--provider", "codex"],
    )
    assert result.exit_code != 0
    assert "provider cost results" in result.output


def test_pricing_config_compatibility_and_backend_propagation(tmp_path):
    cfg = Configuration.from_dict({"main_model": "main", "pricing": PRICING})
    restored = Configuration.from_dict(cfg.to_dict())
    assert (
        restored.to_backend_config(str(tmp_path), str(tmp_path / "docs"), "key").pricing == PRICING
    )
    assert Configuration.from_dict({}).pricing == {}
    with pytest.raises(ValueError):
        validate_pricing({"api": {"m": {"input": True, "output": 1}}})


def test_cache_arithmetic_alias_and_rate_snapshot():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 400,
        "cache_write_tokens": 100,
    }
    priced = price_usage(usage, PRICING, "openai-compatible", "response-alias", "main")
    assert priced["cost_usd"] == pytest.approx((500 + 400 + 100 + 150) / 1_000_000)
    assert priced["pricing_model"] == "main"
    assert priced["rates"] == RATES
    assert price_usage(usage, PRICING, "google", "main", "main") == {"cost_source": "unknown"}
    assert price_usage(None, PRICING, "openai-compatible", "main", "main") == {
        "cost_source": "unknown"
    }
    override = {"openai-compatible": {"main": RATES, "response-alias": {"input": 0, "output": 0}}}
    assert (
        price_usage(usage, override, "openai-compatible", "response-alias", "main")["cost_usd"] == 0
    )


def test_cache_rates_default_to_input():
    priced = price_usage(
        {"input_tokens": 100, "cache_read_tokens": 50, "output_tokens": 10},
        {"api": {"m": {"input": 2, "output": 3}}},
        "api",
        "m",
        "m",
    )
    assert priced["cost_usd"] == pytest.approx(230 / 1_000_000)


def test_openai_usage_aliases_and_cached_details():
    normalized = normalize_api_usage(
        SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        )
    )
    assert normalized["total_tokens"] == 120
    assert normalized["input_tokens"] == 100
    assert normalized["cache_read_tokens"] == 40
    assert "prompt_tokens" not in normalized
    assert normalize_api_usage({"requests": 1}) is None


def test_complete_records_actual_model_and_configured_cost(tmp_path, monkeypatch):
    from codewiki.src.be import llm_services

    backend = PydanticAIBackend(config(tmp_path))
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, prompt_tokens_details={"cached_tokens": 40}
        ),
        model="alias",
    )
    monkeypatch.setattr(
        llm_services,
        "create_openai_client",
        lambda cfg: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply))
        ),
    )
    with backend.usage_ledger.in_phase("overview", "repo"):
        assert backend.complete("hello") == "ok"
    report = backend.usage_ledger.summary()
    assert report["n_calls"] == 1
    assert report["calls"][0]["model"] == "alias"
    assert report["calls"][0]["requested_model"] == "main"
    assert report["totals"]["cost_usd"] == pytest.approx(110 / 1_000_000)
    assert backend.last_usage == report["totals"]


def test_nested_agent_requests_counted_once_with_own_targets(tmp_path):
    ledger = UsageLedger()
    cfg = config(tmp_path)
    child = Agent(UsageModel(FunctionModel(lambda m, i: response(), model_name="main"), "main"))

    async def delegate() -> str:
        with usage_target("child"):
            return (await child.run("child request")).output

    def parent(messages, info):
        if len(messages) == 1:
            result = response()
            result.parts = [ToolCallPart("delegate", {})]
            return result
        return response()

    agent = Agent(UsageModel(FunctionModel(parent, model_name="main"), "main"), tools=[delegate])

    async def run():
        with track_usage(ledger, cfg, "module_agent", "parent") as scope:
            await agent.run("parent request")
            assert scope.totals()["input_tokens"] == 300

    asyncio.run(run())
    report = ledger.summary()
    assert report["n_calls"] == 3
    assert [c["target"] for c in report["calls"]] == ["parent", "child", "parent"]
    assert report["totals"]["total_tokens"] == 360
    assert report["cost_status"] == "complete"


def test_fallback_attribution_and_unknown_usage(tmp_path, monkeypatch):
    from codewiki.src.be import llm_services

    def fail(messages, info):
        raise ModelHTTPError(503, "main", "failed")

    monkeypatch.setattr(
        llm_services, "create_main_model", lambda c: FunctionModel(fail, model_name="main")
    )
    monkeypatch.setattr(
        llm_services,
        "create_fallback_model",
        lambda c: FunctionModel(lambda m, i: response("backup"), model_name="backup"),
    )
    cfg = config(tmp_path, pricing={"openai-compatible": {"backup": RATES}})
    ledger = UsageLedger()

    async def run():
        with track_usage(ledger, cfg, "update_agent", "page"):
            result = await Agent(llm_services.create_fallback_models(cfg)).run("hi")
            assert result.output == "ok"

    asyncio.run(run())
    report = ledger.summary()
    assert report["n_calls"] == 2
    assert report["cost_status"] == "partial"
    assert report["unknown_usage_calls"] == 1
    assert report["unpriced_models"] == ["main"]
    assert report["calls"][1]["pricing_model"] == "backup"


def test_tool_retry_requests_and_failure_after_billed_response(tmp_path):
    from pydantic_ai import ModelRetry

    ledger = UsageLedger()
    cfg = config(tmp_path, pricing={})  # missing prices must not block agent runs
    requests, attempts = 0, 0

    async def sometimes_fails() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelRetry("try again")
        return "done"

    def model(messages, info):
        nonlocal requests
        requests += 1
        result = response()
        if requests <= 2:
            result.parts = [ToolCallPart("sometimes_fails", {})]
        return result

    agent = Agent(
        UsageModel(FunctionModel(model, model_name="main"), "main"),
        tools=[sometimes_fails],
        retries=1,
    )

    async def run():
        with track_usage(ledger, cfg, "module_agent", "page"):
            result = await agent.run("hi")
            assert result.output == "ok"

    asyncio.run(run())
    assert attempts == 2 and requests == 3
    report = ledger.summary()
    assert report["n_calls"] == 3
    assert report["totals"]["input_tokens"] == 300
    assert report["cost_status"] == "unknown"

    async def fail() -> str:
        raise RuntimeError("tool failed after billing")

    def failing_model(messages, info):
        result = response()
        result.parts = [ToolCallPart("fail", {})]
        return result

    ledger = UsageLedger()
    agent = Agent(UsageModel(FunctionModel(failing_model, model_name="main"), "main"), tools=[fail])

    async def failing_run():
        with track_usage(ledger, config(tmp_path), "module_agent", "page"):
            await agent.run("hi")

    with pytest.raises(RuntimeError, match="tool failed after billing"):
        asyncio.run(failing_run())
    assert ledger.summary()["totals"]["cost_usd"] > 0
    assert ledger.summary()["n_calls"] == 1


def test_zero_unknown_partial_and_small_cost_output(capsys):
    ledger = UsageLedger()
    _echo_usage({"usage_report": ledger.summary()})
    assert "$0.00" in capsys.readouterr().out
    ledger.add(
        "request",
        "a",
        {"input_tokens": 1, "cost_usd": 0.000001},
        model="priced",
        cost_source="configured",
    )
    ledger.add("request", "b", {"input_tokens": 1}, model="unpriced")
    _echo_usage({"usage_report": ledger.summary()})
    output = capsys.readouterr().out
    assert "$0.000001" in output and "partial" in output
    assert "By model" in output and "By phase" in output
    assert "codewiki config pricing set" in output
    zero = UsageLedger()
    zero.add("request", "", {"input_tokens": 10, "cost_usd": 0}, cost_source="configured")
    assert zero.summary()["cost_status"] == "complete"
    assert not zero.summary()["unpriced_models"]


def adapter(tmp_path, ledger):
    instance = object.__new__(CLIDocumentationGenerator)
    instance._usage_ledger = ledger
    instance.output_dir = tmp_path
    instance.config = {"provider": "openai-compatible"}
    instance.job = SimpleNamespace(statistics=JobStatistics())
    return instance


def test_failure_report_preserves_metadata_and_raises_original(tmp_path, monkeypatch, capsys):
    ledger = UsageLedger()
    ledger.add(
        "request", "", {"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.001}, model="main"
    )
    (tmp_path / "metadata.json").write_text(json.dumps({"generation_info": {"commit_id": "old"}}))
    instance = CLIDocumentationGenerator(tmp_path, tmp_path, config(tmp_path).__dict__)

    async def fail(cfg):
        instance._usage_ledger = ledger
        raise APIError("original failure")

    monkeypatch.setattr(instance, "_run_backend_generation", fail)
    with pytest.raises(APIError, match="original failure"):
        instance.generate()
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["generation_info"]["commit_id"] == "old"
    assert metadata["usage"]["incomplete"] is True
    assert metadata["usage"]["totals"]["cost_usd"] == 0.001
    assert "Run incomplete" in capsys.readouterr().out


def test_final_report_and_update_history_are_one_result_per_run(tmp_path):
    instance = adapter(tmp_path, UsageLedger())
    first = UpdateRecord()
    instance._last_update_record = first
    instance._finish_usage()
    instance._finish_usage()
    second = UpdateRecord()
    instance._last_update_record = second
    instance._finish_usage()
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    record = json.loads((tmp_path / "update_record.json").read_text())
    assert len(metadata["update_history"]) == 2
    assert first.run_id != second.run_id
    assert record["usage_report"] == metadata["usage"] == metadata["last_update"]["usage"]
    assert instance.job.statistics.total_tokens_used == 0


def test_inclusive_token_statistics_and_resumed_run(tmp_path):
    ledger = UsageLedger()
    ledger.add(
        "request",
        "",
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 40,
            "cache_write_tokens": 10,
        },
    )
    instance = adapter(tmp_path, ledger)
    instance._record_usage_statistics()
    assert instance.job.statistics.total_tokens_used == 120
    instance._usage_ledger = UsageLedger()
    instance._record_usage_statistics()
    assert instance.job.statistics.total_tokens_used == 0
    assert instance.job.statistics.total_cost_usd == 0


def test_full_fallback_keeps_attempt_costs_and_prior_history(tmp_path):
    output = tmp_path / "docs"
    output.mkdir()
    (output / "metadata.json").write_text(json.dumps({"update_history": [{"run_id": "old"}]}))
    ledger = UsageLedger()
    instance = adapter(output, ledger)
    instance._last_update_record = UpdateRecord(outcome="full_fallback")
    instance.commit_id = "new"
    instance.verbose = False
    with ledger.in_phase("update"):
        ledger.add("routing", "", {"input_tokens": 100, "cost_usd": 0.001}, model="cluster")
    instance._move_docs_aside()
    with ledger.in_phase("modules"):
        ledger.add("module_agent", "", {"input_tokens": 200, "cost_usd": 0.002}, model="main")
    instance._merge_update_summary(str(output))
    instance._finish_usage()
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["usage"]["totals"]["cost_usd"] == pytest.approx(0.003)
    assert metadata["usage"]["n_calls"] == 2
    assert len(metadata["update_history"]) == 2
    assert metadata["update_history"][0]["run_id"] == "old"
    assert metadata["last_update"]["usage"] == metadata["usage"]
    assert metadata["last_update"]["previous_docs"] == instance._preserved_docs_dir


def test_direct_generation_failure_saves_available_usage(tmp_path):
    from codewiki.src.be.documentation_generator import DocumentationGenerator

    cfg = config(tmp_path)
    backend = PydanticAIBackend(cfg)
    backend.usage_ledger.add("request", "", {"input_tokens": 10, "cost_usd": 0.0001})
    generator = object.__new__(DocumentationGenerator)
    generator.config = cfg
    generator.backend = backend

    def fail():
        raise RuntimeError("analysis failed")

    generator.graph_builder = SimpleNamespace(build_dependency_graph=fail)
    with pytest.raises(RuntimeError, match="analysis failed"):
        asyncio.run(generator.run())
    metadata = json.loads((tmp_path / "docs" / "metadata.json").read_text())
    assert metadata["usage"]["incomplete"] is True
    assert metadata["usage"]["totals"]["cost_usd"] == 0.0001
