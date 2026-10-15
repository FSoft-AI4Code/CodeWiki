"""Usage ledger: every LLM call of a run, summed into tokens and cost."""

import json
import threading
from types import SimpleNamespace

from caw.models import UsageStats

from codewiki.cli.utils.instructions import _echo_usage
from codewiki.src.be import caw_backend
from codewiki.src.be.backend import UsageLedger
from codewiki.src.be.caw_backend import CawBackend
from codewiki.src.be.documentation_generator import DocumentationGenerator


def test_summary_totals_and_phases():
    ledger = UsageLedger()
    with ledger.in_phase("clustering"):
        ledger.add("complete", "", {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.5})
    with ledger.in_phase("modules"):
        ledger.add(
            "module_agent", "auth", {"input_tokens": 200, "output_tokens": 20, "cost_usd": 1.0}
        )
        ledger.add(
            "module_agent", "db", {"input_tokens": 300, "output_tokens": 30, "cost_usd": 1.5}
        )

    summary = ledger.summary()
    assert summary["n_calls"] == 3
    assert summary["totals"] == {"input_tokens": 600, "output_tokens": 60, "cost_usd": 3.0}
    assert summary["by_phase"]["clustering"]["calls"] == 1
    assert summary["by_phase"]["modules"] == {
        "calls": 2,
        "input_tokens": 500,
        "output_tokens": 50,
        "cost_usd": 2.5,
    }
    assert summary["unpriced_models"] == []
    assert [c["target"] for c in summary["calls"]] == ["", "auth", "db"]


def test_in_phase_restores_and_labels_target():
    ledger = UsageLedger()
    with ledger.in_phase("overview", target="repo"):
        ledger.add("complete", "", None)
    ledger.add("complete", "", None)
    calls = ledger.summary()["calls"]
    assert (calls[0]["phase"], calls[0]["target"]) == ("overview", "repo")
    assert (calls[1]["phase"], calls[1]["target"]) == ("generate", "")


def test_tokens_without_cost_are_unpriced():
    ledger = UsageLedger()
    ledger.add("module_agent", "a", {"input_tokens": 10, "cost_usd": 0.0}, model="gpt-x")
    ledger.add("module_agent", "b", {"input_tokens": 10}, model="")
    ledger.add("module_agent", "c", None, model="ignored")  # no usage reported at all
    assert ledger.summary()["unpriced_models"] == ["<default>", "gpt-x"]


def test_add_is_thread_safe():
    ledger = UsageLedger()

    def worker():
        for _ in range(500):
            ledger.add("module_agent", "m", {"input_tokens": 1, "cost_usd": 0.001})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    summary = ledger.summary()
    assert summary["n_calls"] == 4000
    assert summary["totals"]["input_tokens"] == 4000


def _caw_backend(monkeypatch, usage: UsageStats) -> CawBackend:
    class FakeCawAgent:
        def __init__(self, **kwargs):
            self.model = kwargs.get("model")

        def completion(self, prompt):
            return SimpleNamespace(
                result="ok",
                total_usage=usage,
                model="opus",
                num_turns=1,
                total_tool_calls=0,
            )

    monkeypatch.setattr(caw_backend, "CawAgent", FakeCawAgent)
    backend = object.__new__(CawBackend)  # skip the CLI-on-PATH check
    backend._caw_provider = "claude_code"
    backend._model = None
    return backend


def test_caw_complete_records_usage(monkeypatch):
    usage = UsageStats(input_tokens=1000, output_tokens=200, cache_read_tokens=50, cost_usd=0.12)
    backend = _caw_backend(monkeypatch, usage)

    with backend.usage_ledger.in_phase("overview", target="repo"):
        assert backend.complete("prompt") == "ok"

    assert backend.last_usage["cost_usd"] == 0.12
    (call,) = backend.usage_ledger.summary()["calls"]
    assert call["phase"] == "overview"
    assert call["target"] == "repo"
    assert call["kind"] == "complete"
    assert call["model"] == "opus"
    assert call["input_tokens"] == 1000
    assert call["cost_usd"] == 0.12


def test_metadata_includes_usage(tmp_path, monkeypatch):
    backend = _caw_backend(monkeypatch, UsageStats(input_tokens=10, cost_usd=0.25))
    backend.complete("prompt")

    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        main_model="opus", provider="claude-code", repo_path=".", max_depth=2, layout="flat"
    )
    gen.commit_id = None
    gen.backend = backend
    gen.create_documentation_metadata(str(tmp_path), {}, 0)

    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["generation_info"]["provider"] == "claude-code"
    assert metadata["usage"]["n_calls"] == 1
    assert metadata["usage"]["totals"]["cost_usd"] == 0.25


def test_cli_prints_tokens_and_cost(capsys):
    _echo_usage(
        {
            "total_tokens_used": 1_250_000,
            "usage_totals": {"input_tokens": 1_000_000, "output_tokens": 250_000},
            "total_cost_usd": 4.2,
            "unpriced_models": [],
            "provider": "claude-code",
        }
    )
    out = capsys.readouterr().out
    assert "1,250,000 (in 1.0M / out 250.0k)" in out
    assert "$4.20 (API-equivalent; billed to your subscription)" in out


def test_cli_flags_unpriced_codex_models(capsys):
    _echo_usage(
        {
            "total_tokens_used": 10,
            "usage_totals": {"input_tokens": 10},
            "total_cost_usd": 0.0,
            "unpriced_models": ["gpt-x"],
            "provider": "codex",
        }
    )
    out = capsys.readouterr().out
    assert "Est. cost" not in out
    assert "Cost unknown for:  gpt-x" in out
    assert "caw pricing set codex" in out


def test_cli_silent_without_usage(capsys):
    _echo_usage({"total_tokens_used": 0})
    assert capsys.readouterr().out == ""
