"""The ask pipeline, change-impact analysis, stats and logging."""

import json
import sys

import pytest

from codewiki.mcp.tools import qa as qa_tools
from codewiki.src.be.qa import prompts as P
from codewiki.src.be.qa.engine import QAEngine, parse_json
from codewiki.src.be.qa.impact import find_references
from codewiki.src.be.qa.index import QAIndex
from codewiki.src.be.qa.sessions import SessionStore
from codewiki.src.be.qa.stats import AskStats
from qa_fixture import build

UNSIGN = "pkg/timed.py::TimestampSigner.unsign"
USAGE = {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5, "cost_usd": 0.001}


class ScriptedBackend:
    """Replies by prompt type; records every call."""

    def __init__(self, plan, rounds=(), answer="It checks the age. `pkg/timed.py:L16-L22`"):
        self.plan = plan
        self.rounds = list(rounds)
        self.answer = answer
        self.calls: list[tuple[str, str, bool]] = []
        self.last_usage = None

    def complete(self, prompt, *, model=None, system_prompt=None, allow_tools=True):
        self.calls.append((system_prompt, prompt, allow_tools))
        self.last_usage = USAGE
        if system_prompt == P.PLAN_SYSTEM:
            return self.plan if isinstance(self.plan, str) else json.dumps(self.plan)
        if system_prompt == P.ROUND_SYSTEM:
            reply = self.rounds.pop(0) if self.rounds else {"keep": [], "confidence": 50}
            return json.dumps(reply)
        return self.answer

    def prompts(self, system):
        return [p for s, p, _ in self.calls if s == system]


@pytest.fixture
def docs(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEWIKI_QA_LOG_DIR", str(tmp_path / "logs"))
    return build(tmp_path)


def engine(docs_dir, backend, **kw):
    return QAEngine(QAIndex(str(docs_dir)), backend, sessions=SessionStore(), **kw)


def test_parse_json_tolerates_fences_prose_and_trailing_commas():
    assert parse_json('Sure!\n```json\n{"a": [1, 2,], "b": 3,}\n```') == {"a": [1, 2], "b": 3}
    assert parse_json("no json here") is None
    assert parse_json('{"x": {"y": 1}} trailing') == {"x": {"y": 1}}


def test_fast_path_answers_in_two_calls(docs):
    _, docs_dir = docs
    backend = ScriptedBackend({"intent": "explain", "keep": [UNSIGN], "confidence": 95})
    result = engine(docs_dir, backend).ask("How does expiry work?")
    assert len(backend.calls) == 2
    assert all(allow is False for _, _, allow in backend.calls)  # the CLI must not open files
    assert result.stats.stop_reason == "confident" and result.stats.rounds == 1
    answer_prompt = backend.prompts(P.ANSWER_SYSTEM)[0]
    assert "def unsign(self, signed_value, max_age=MAX_AGE)" in answer_prompt
    assert "def legacy_window" not in answer_prompt  # the method body, not the whole file
    assert result.sources[0]["id"] == UNSIGN
    s = result.stats
    assert (s.llm_calls, s.input_tokens, s.output_tokens, s.cache_read_tokens) == (2, 210, 40, 10)
    assert s.cost_usd == pytest.approx(0.002)
    assert s.footer().startswith("Answered in ") and "2 LLM call(s)" in s.footer()
    assert "250 tokens (210 in / 40 out, 10 cached)" in s.footer()


def test_two_rounds_and_the_rewrite_is_actually_used(docs):
    _, docs_dir = docs
    plan = {
        "intent": "locate",
        "rewritten_query": "get_timestamp current unix time",
        "keywords": ["get_timestamp"],
        "keep": [],
        "confidence": 30,
    }
    backend = ScriptedBackend(plan, rounds=[{"keep": [UNSIGN], "confidence": 90}])
    result = engine(docs_dir, backend).ask("Where does it read the clock?")
    assert result.stats.rounds == 2 and result.stats.stop_reason == "confident"
    assert len(backend.calls) == 3
    # the raw question never names get_timestamp; only the rewrite does (FastCode drops it)
    round_prompt = backend.prompts(P.ROUND_SYSTEM)[0]
    assert "TimestampSigner.get_timestamp" in round_prompt
    # scouting prompts carry metadata only, never bodies
    assert "return int(time.time())" not in round_prompt
    assert "return int(time.time())" not in backend.prompts(P.PLAN_SYSTEM)[0]


@pytest.mark.parametrize(
    "rounds,kwargs,reason",
    [
        ([{"keep": [UNSIGN], "confidence": 50}], {}, "no more leads"),
        (
            [
                {"keep": [UNSIGN], "confidence": 41, "search": ["x"]},
                {"keep": [UNSIGN], "confidence": 42, "search": ["y"]},
            ],
            {"max_rounds": 5},
            "low gain",
        ),
        (
            [{"keep": [UNSIGN], "confidence": 60, "search": ["sign"]}] * 3,
            {"max_rounds": 2},
            "max rounds",
        ),
    ],
)
def test_stop_rules(docs, rounds, kwargs, reason):
    _, docs_dir = docs
    backend = ScriptedBackend({"keep": [], "confidence": 40}, rounds=rounds)
    result = engine(docs_dir, backend, **kwargs).ask("How does expiry work?")
    assert result.stats.stop_reason == reason


def test_budget_stop_and_cap(docs):
    _, docs_dir = docs
    backend = ScriptedBackend(
        {"keep": [], "confidence": 10},
        rounds=[{"keep": [UNSIGN, "pkg/signer.py::Signer"], "confidence": 50, "search": ["x"]}],
    )
    result = engine(docs_dir, backend).ask("How does expiry work?", max_context_tokens=150)
    assert result.stats.stop_reason == "budget"
    assert result.stats.context_tokens <= 160


def test_unparseable_plan_falls_back_to_keyword_search(docs):
    _, docs_dir = docs
    backend = ScriptedBackend("I cannot comply with JSON today.")
    result = engine(docs_dir, backend).ask("How does TimestampSigner unsign expire values?")
    assert result.answer and result.sources  # still answered from the BM25 candidates
    assert any("timed" in s["location"].lower() for s in result.sources)


def test_impact_question_uses_the_reference_finder(docs):
    _, docs_dir = docs
    plan = {
        "intent": "impact",
        "impact": {"symbol": "MAX_AGE", "file": "pkg/timed.py", "change": "rename"},
        "keep": [],
        "confidence": 20,
    }
    backend = ScriptedBackend(plan, rounds=[{"keep": [UNSIGN], "confidence": 90}])
    result = engine(docs_dir, backend).ask("What breaks if I rename MAX_AGE?")
    assert result.impact and "tests/test_timed.py" in result.impact
    answer_prompt = backend.prompts(P.ANSWER_SYSTEM)[0]
    assert "Impact scan" in answer_prompt and "Risk" in answer_prompt


def test_follow_up_sees_the_previous_summary(docs):
    _, docs_dir = docs
    store = SessionStore()
    backend = ScriptedBackend(
        {"keep": [UNSIGN], "confidence": 95},
        answer="Answer one.\n<SUMMARY>read unsign; expiry uses MAX_AGE</SUMMARY>",
    )
    eng = QAEngine(QAIndex(str(docs_dir)), backend, sessions=store)
    first = eng.ask("How does expiry work?")
    assert "<SUMMARY>" not in first.answer and first.answer == "Answer one."
    eng.ask("And what is the default?", session_id=first.session_id)
    plan_prompt = backend.prompts(P.PLAN_SYSTEM)[1]
    assert "Earlier in this conversation" in plan_prompt and "expiry uses MAX_AGE" in plan_prompt


def test_keep_turns_forgets_answers_after_an_edited_question(docs):
    _, docs_dir = docs
    store = SessionStore()
    backend = ScriptedBackend(
        {"keep": [UNSIGN], "confidence": 95},
        answer="Answer.\n<SUMMARY>expiry uses MAX_AGE</SUMMARY>",
    )
    eng = QAEngine(QAIndex(str(docs_dir)), backend, sessions=store)
    first = eng.ask("How does expiry work?")
    second = eng.ask("And the default?", session_id=first.session_id)
    assert (first.turns, second.turns) == (1, 2)
    assert first.to_dict()["turns"] == 1
    # The chat edited the first question: nothing it remembered should survive.
    redo = eng.ask("How does signing work?", session_id=first.session_id, keep_turns=0)
    assert "Earlier in this conversation" not in backend.prompts(P.PLAN_SYSTEM)[-1]
    assert redo.turns == 1 and redo.session_id == first.session_id


def test_should_stop_cancels_before_the_next_llm_call_and_remembers_nothing(docs):
    from codewiki.src.be.qa.engine import AskCancelled

    _, docs_dir = docs
    store = SessionStore()
    backend = ScriptedBackend(
        {"keep": [], "confidence": 30},
        answer="Answer.\n<SUMMARY>s</SUMMARY>",
    )
    eng = QAEngine(QAIndex(str(docs_dir)), backend, sessions=store)
    statuses: list[str] = []
    with pytest.raises(AskCancelled):
        eng.ask(
            "How does expiry work?",
            session_id="s1",
            on_event=lambda e: statuses.append(e["message"]),
            should_stop=lambda: len(backend.calls) >= 1,  # stop once the plan call ran
        )
    assert len(backend.calls) == 1 and len(statuses) == 1
    assert store.turn_count("s1") == 0

    # Stopped while the answer was being written: the reply is dropped too.
    def stop_during_answer():
        return any(s == P.ANSWER_SYSTEM for s, _, _ in backend.calls)

    backend.calls.clear()
    backend.plan = {"keep": [UNSIGN], "confidence": 95}
    with pytest.raises(AskCancelled):
        eng.ask("How does expiry work?", session_id="s1", should_stop=stop_during_answer)
    assert store.turn_count("s1") == 0


def test_status_steps_say_what_was_found(docs):
    _, docs_dir = docs
    backend = ScriptedBackend({"intent": "explain", "keep": [UNSIGN], "confidence": 95})
    statuses: list[str] = []
    engine(docs_dir, backend).ask(
        "How does expiry work?", on_event=lambda e: statuses.append(e["message"])
    )
    assert statuses[0] == "Searching the docs and code…"
    assert statuses[1].startswith("Found ") and "explain question" in statuses[1]
    assert statuses[-1].startswith("Reading ")


def test_stale_docs_are_flagged_in_the_prompt_and_answer(docs):
    repo, docs_dir = docs
    timed = repo / "pkg" / "timed.py"
    timed.write_text(timed.read_text() + "\n\ndef extra():\n    return 1\n")
    backend = ScriptedBackend({"keep": ["Timed.md#expiry-check", UNSIGN], "confidence": 95})
    result = engine(docs_dir, backend).ask("How does expiry work?")
    assert "[stale:" in backend.prompts(P.ANSWER_SYSTEM)[0]
    assert "older than the code" in result.answer
    assert result.stats.stale_files == 1 and "1 file(s) changed" in result.freshness


def test_query_log_written_outside_docs(docs, tmp_path):
    _, docs_dir = docs
    backend = ScriptedBackend({"keep": [UNSIGN], "confidence": 95})
    engine(docs_dir, backend).ask("How does expiry work?")
    engine(docs_dir, backend, log_questions=False).ask("Secret question?")
    logs = list((tmp_path / "logs").glob("qa-*.jsonl"))
    assert len(logs) == 1 and not list(docs_dir.rglob("qa-*.jsonl"))
    lines = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert lines[0]["question"] == "How does expiry work?" and lines[0]["llm_calls"] == 2
    assert "question" not in lines[1]


def test_stats_usage_key_variants():
    s = AskStats()
    s.add_usage({"prompt_tokens": 1500, "completion_tokens": 300})
    s.add_usage(None)
    assert (s.llm_calls, s.total_tokens) == (2, 1800)
    assert "1.8k tokens (1.5k in / 300 out)" in s.finish().footer()


# --- impact finder ------------------------------------------------------------


def test_impact_labels_sites_and_excludes_noise(docs):
    _, docs_dir = docs
    r = find_references(QAIndex(str(docs_dir)), "MAX_AGE", file="pkg/timed.py", change="rename")
    by_kind = {}
    for site in r.usages:
        by_kind.setdefault(site.kind, []).append(site)
    assert any(s.file == "pkg/timed.py" for s in r.definitions)
    assert {s.file for s in by_kind["read"]} >= {"pkg/timed.py"}  # default arg + default_age
    assert any(s.file == "pkg/__init__.py" for s in by_kind["import"])
    assert any(s.file == "config/settings.yaml" for s in by_kind["config"])
    assert any(s.file == "pkg/__init__.py" for s in r.public_api)  # __all__ + package import
    assert any("getattr" in s.text for s in r.dynamic)
    # the same-named local in legacy_window and the comment are not usages
    assert all("legacy_window" not in (s.component or "") for s in r.usages)
    assert any("legacy_window" in (s.component or "") for s in r.other_scope)
    assert any(s.text.startswith("# MAX_AGE") for s in r.textual)
    assert r.tests == ["tests/test_timed.py"]
    assert "pkg/timed.py::default_age" in r.affected
    assert "Timed.md" in r.doc_pages


def test_impact_of_a_parameter_finds_keyword_call_sites(docs):
    repo, docs_dir = docs
    (repo / "pkg" / "use.py").write_text(
        "from pkg.timed import TimestampSigner\n\n\n"
        "def check(v):\n    return TimestampSigner('k').unsign(v, max_age=5)\n"
    )
    r = find_references(QAIndex(str(docs_dir)), "TimestampSigner.unsign.max_age", change="remove")
    assert any(s.kind == "definition" and s.file == "pkg/timed.py" for s in r.definitions)
    assert any(s.kind == "keyword" and s.file == "pkg/use.py" for s in r.usages)
    assert "pkg/use.py" in r.stale_files  # a new file, not in the docs' commit
    assert "changed since the docs" in r.render()


def test_mcp_impact_and_ask(docs, monkeypatch):
    _, docs_dir = docs
    out = qa_tools.handle_impact({"docs_dir": str(docs_dir), "symbol": "MAX_AGE"})
    assert "Usages" in out and "tests/test_timed.py" in out
    with pytest.raises(ValueError):
        qa_tools.handle_impact({"docs_dir": str(docs_dir), "symbol": "x", "change": "delete"})

    backend = ScriptedBackend({"keep": [UNSIGN], "confidence": 95})
    import codewiki.src.be.qa as qa_pkg

    monkeypatch.setattr(qa_pkg, "load_backend", lambda index, model=None: backend)
    out = qa_tools.handle_ask({"docs_dir": str(docs_dir), "question": "How does expiry work?"})
    assert "It checks the age." in out and "Answered in" in out and "session" in out


def test_cli_ask_prints_answer_sources_and_stats(docs, monkeypatch):
    from click.testing import CliRunner

    import codewiki.src.be.qa as qa_pkg
    from codewiki.cli.main import cli

    _, docs_dir = docs
    backend = ScriptedBackend({"keep": [UNSIGN], "confidence": 95})
    monkeypatch.setattr(qa_pkg, "load_backend", lambda index, model=None: backend)
    res = CliRunner().invoke(
        cli, ["ask", "How", "does", "expiry", "work?", "--docs", str(docs_dir)]
    )
    assert res.exit_code == 0, res.output
    assert "It checks the age." in res.output and "Sources:" in res.output
    assert "Answered in" in res.output and "--session" in res.output
    as_json = CliRunner().invoke(cli, ["ask", "expiry?", "--docs", str(docs_dir), "--json"])
    data = json.loads(as_json.stdout)
    assert data["stats"]["llm_calls"] == 2 and data["sources"][0]["id"] == UNSIGN


def test_anthropic_style_usage_counts_cached_prompt_tokens():
    s = AskStats()
    s.add_usage(
        {
            "input_tokens": 4,
            "output_tokens": 1600,
            "cache_read_tokens": 4500,
            "cache_write_tokens": 300,
        }
    )
    assert (s.input_tokens, s.cache_read_tokens) == (4804, 4500)


def test_a_method_inside_a_selected_class_is_not_read_twice(docs):
    from codewiki.src.be.qa.retrieve import Retriever

    _, docs_dir = docs
    r = Retriever(QAIndex(str(docs_dir)))
    items = r.assemble([UNSIGN, "pkg/timed.py::TimestampSigner"], 10_000)
    assert [i.id for i in items] == ["pkg/timed.py::TimestampSigner"]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a shebang script")
def test_caw_plain_completion_runs_claude_without_tools_in_a_neutral_dir(tmp_path, monkeypatch):
    import os
    import stat

    from codewiki.src.be.caw_backend import CawBackend, neutral_cwd

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "call.json"
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}}, open({str(record)!r}, 'w'))\n"
        "print('mise noise on stdout')\n"
        "print(json.dumps({'result': 'pong', 'is_error': False, 'total_cost_usd': 0.01,"
        " 'usage': {'input_tokens': 7, 'output_tokens': 3, 'cache_read_input_tokens': 400,"
        " 'cache_creation_input_tokens': 50}}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    backend = object.__new__(CawBackend)
    backend._caw_provider, backend._model = "claude_code", "m1"
    assert backend.complete("hello", system_prompt="sys", allow_tools=False) == "pong"
    call = json.loads(record.read_text())
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--system-prompt") + 1] == "sys" and "m1" in argv
    assert call["stdin"] == "hello"
    # macOS: the temp dir is under /var, a symlink to /private/var
    assert os.path.realpath(call["cwd"]) == os.path.realpath(neutral_cwd())
    s = AskStats()
    s.add_usage(backend.last_usage)
    assert (s.input_tokens, s.cache_read_tokens, s.cost_usd) == (457, 400, 0.01)
