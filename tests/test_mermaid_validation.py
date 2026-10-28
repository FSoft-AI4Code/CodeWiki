"""Mermaid syntax, repair persistence and inconclusive validation (issue #141)."""

import asyncio
import json
from pathlib import Path

import pytest

from codewiki.src.be import utils
from codewiki.src.be.mermaid_support import (
    DiagramResult,
    MERMAID_GUIDELINES,
    mermaid_blocks,
    quote_flowchart_labels,
)

FIXTURES = json.loads((Path(__file__).parent / "mermaid/fixtures.json").read_text())
INVALID = "flowchart TD\nC{msg.error()?}"
FIXED = 'flowchart TD\nC{"msg.error()?"}'
PARSE_ERROR = "Error: Parse error on line 2: expected DIAMOND_STOP, got 'PS'"


def markdown(diagram=INVALID):
    return f"```mermaid\n{diagram}\n```\n"


@pytest.fixture
def parser(monkeypatch):
    calls = []

    async def parse(source):
        calls.append(source)
        if INVALID in source.replace("\r\n", "\n"):
            return DiagramResult("invalid", PARSE_ERROR)
        if "broken" in source:
            return DiagramResult("invalid", "Parse error on line 2: got 'TEXT'")
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    return calls


@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: case["name"])
def test_repair_fixtures_match_real_parser_expectations(case):
    repaired, changes = quote_flowchart_labels(case["source"])
    assert repaired == case.get("repaired", case["source"])
    assert bool(changes) == ("repaired" in case)
    assert quote_flowchart_labels(repaired) == (repaired, [])


def test_read_only_validation_reports_absolute_lines(parser):
    async def check():
        original = markdown()
        report = await utils.validate_mermaid_content(original, repair=False)
        assert report.content == original
        assert report.errors and not report.repairs
        assert "line 3" in report.errors[0]
        assert "got" in report.errors[0]
        assert MERMAID_GUIDELINES in report.format("x.md")

    asyncio.run(check())
    assert len(parser) == 1


def test_repairs_only_after_failure_and_persists_exact_bytes(tmp_path, parser):
    path = tmp_path / "x.md"
    before = (
        ("# Intro\r\n\r\n" + markdown() + "\n```python\nC{msg.error()?}\n```\n")
        .replace("\n", "\r\n")
        .replace("\r\r\n", "\r\n")
        .encode()
    )
    path.write_bytes(before)
    readonly = asyncio.run(utils.validate_mermaid_diagrams(str(path), "x.md"))
    assert "syntax errors" in readonly
    assert path.read_bytes() == before
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "repaired and validated" in result
    assert "fence line 3" in result
    assert path.read_bytes() == before.replace(
        INVALID.replace("\n", "\r\n").encode(), FIXED.replace("\n", "\r\n").encode(), 1
    )
    after = path.read_bytes()
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "syntax correct" in result and "repaired" not in result
    assert path.read_bytes() == after


@pytest.mark.parametrize("status", ["invalid", "unavailable"])
def test_failed_revalidation_does_not_save_candidate(tmp_path, monkeypatch, status):
    path = tmp_path / "x.md"
    path.write_text(markdown())
    original = path.read_bytes()
    results = iter([DiagramResult("invalid", PARSE_ERROR), DiagramResult(status, "still broken")])

    async def parse(_):
        return next(results)

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "syntax errors" in result and "repaired and validated" not in result
    assert path.read_bytes() == original
    if status == "unavailable":
        assert "revalidation unavailable" in result


def test_concurrent_change_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "x.md"
    path.write_text(markdown())

    async def parse(source):
        if INVALID in source.replace("\r\n", "\n"):
            return DiagramResult("invalid", PARSE_ERROR)
        path.write_text("concurrent edit")
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "File changed" in result and "repaired and validated" not in result
    assert path.read_text() == "concurrent edit"


def test_multiple_diagrams_and_non_mermaid_fences(parser):
    original = (
        "# Title\n````markdown\n"
        + markdown()
        + "````\n"
        + markdown()
        + "\n~~~mermaid\n"
        + INVALID
        + "\n~~~\n"
    )
    report = asyncio.run(utils.validate_mermaid_content(original, repair=True))
    assert report.diagrams == 2 and len(report.repairs) == 2
    assert report.content.count(FIXED) == 2
    assert report.content.count(INVALID) == 1  # example inside outer Markdown fence
    assert len(parser) == 4
    assert len(utils.extract_mermaid_blocks(original)) == 2


def test_unclosed_fence_and_other_languages_are_not_repaired(parser):
    original = "```mermaidx\n" + INVALID + "\n```\n```mermaid\n" + INVALID
    report = asyncio.run(utils.validate_mermaid_content(original, repair=True))
    assert report.diagrams == 1 and report.errors
    assert report.content == original and not report.repairs
    assert not mermaid_blocks("```python\n" + INVALID + "\n```")


def test_mixed_results_do_not_claim_all_valid(monkeypatch):
    results = iter(
        [
            DiagramResult("valid"),
            DiagramResult("invalid", PARSE_ERROR.replace("'PS'", "'TEXT'")),
            DiagramResult("unavailable", "offline"),
        ]
    )

    async def parse(_):
        return next(results)

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    report = asyncio.run(utils.validate_mermaid_content(markdown() * 3, repair=True))
    # First diagram stays untouched even though the source looks repairable.
    assert report.content == markdown() * 3
    assert len(report.errors) == 1 and report.unavailable
    message = report.format("mixed.md")
    assert "syntax errors" in message and "unavailable" in message
    assert "are syntax correct" not in message


@pytest.mark.parametrize("disabled", [True, False])
def test_disabled_or_timed_out_validation_is_unavailable(monkeypatch, disabled):
    async def no_local(_):
        return None

    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", no_local)
    monkeypatch.setattr(utils, "_MERMAID_PY_BROKEN", disabled)
    calls = []

    async def timeout(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise asyncio.TimeoutError

    def ready(*args):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    monkeypatch.setattr(utils, "_run_in_daemon_thread", ready)
    monkeypatch.setattr(utils.asyncio, "wait_for", timeout)
    report = asyncio.run(utils.validate_mermaid_content(markdown() * 2, repair=True))
    assert len(report.unavailable) == 2 and not report.errors and not report.repairs
    assert report.content == markdown() * 2
    assert "syntax correct" not in report.format("x.md")
    assert calls == ([] if disabled else [15.0])


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (400, PARSE_ERROR, "invalid"),
        (503, PARSE_ERROR, "unavailable"),
        (429, "Too many requests", "unavailable"),
        (400, "Bad parameters", "unavailable"),
    ],
)
def test_remote_adapter_classifies_http_responses(monkeypatch, code, message, expected):
    import requests

    response = requests.Response()
    response.status_code = code
    response._content = message.encode()
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    assert utils._parse_via_mermaid_py(INVALID).status == expected


def test_remote_success_and_connection_error(monkeypatch):
    import requests

    response = requests.Response()
    response.status_code = 200
    response._content = b"<svg></svg>"
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    assert utils._parse_via_mermaid_py(FIXED).status == "valid"

    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", offline)
    assert utils._parse_via_mermaid_py(FIXED) == DiagramResult("unavailable", "offline")


def test_remote_diagnostics_keep_final_lexer_token(monkeypatch):
    import requests

    response = requests.Response()
    response.status_code = 400
    response._content = ("Parse error on line 2: " + "expected token, " * 50 + "got 'PS'").encode()
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    result = utils._parse_via_mermaid_py(INVALID)
    assert result.status == "invalid" and "got 'PS'" in result.message


@pytest.mark.parametrize("local", ["", PARSE_ERROR, None])
def test_local_parser_success_error_and_fallback(monkeypatch, local):
    async def parse(_):
        return local

    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", parse)
    monkeypatch.setattr(utils, "_MERMAID_PY_BROKEN", False)
    calls = []

    def remote(source):
        calls.append(source)
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_parse_via_mermaid_py", remote)
    result = asyncio.run(utils._validate_diagram(INVALID))
    assert result.status == ("invalid" if local else "valid")
    assert len(calls) == (1 if local is None else 0)


def test_missing_dependency_and_legacy_warning(monkeypatch):
    async def no_local(_):
        return None

    def missing(_):
        raise ImportError("mermaid not installed")

    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", no_local)
    monkeypatch.setattr(utils, "_parse_via_mermaid_py", missing)
    monkeypatch.setattr(utils, "_MERMAID_PY_BROKEN", False)
    result = asyncio.run(utils.validate_single_diagram(FIXED, 1, 1))
    assert "validation unavailable" in result and "not installed" in result


def test_empty_markdown_does_not_use_parser(parser):
    report = asyncio.run(utils.validate_mermaid_content("# No diagrams"))
    assert report.format("x.md") == "No mermaid diagrams found in the file"
    assert not parser


def test_valid_result_never_triggers_repair(monkeypatch):
    calls = []

    async def valid(source):
        calls.append(source)
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", valid)
    report = asyncio.run(utils.validate_mermaid_content(markdown(), repair=True))
    assert not report.errors and not report.repairs
    assert report.content == markdown() and len(calls) == 1
