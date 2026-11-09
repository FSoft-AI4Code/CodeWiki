"""Grammars from tree-sitter-language-pack: a failed download stops the
analysis once, with a clear message, and the Docker image fetches every
grammar the analyzers load."""

import re
from pathlib import Path

import pytest
import tree_sitter_language_pack as tslp

from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import (
    CallGraphAnalyzer,
)
from codewiki.src.be.dependency_analyzer.utils import grammars
from codewiki.src.be.dependency_analyzer.utils.grammars import (
    PACK_GRAMMARS,
    GrammarUnavailableError,
    ensure_pack_grammars,
    get_pack_parser,
)

DOCKERFILE = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"


@pytest.fixture
def offline(monkeypatch):
    """Every pack grammar load fails as it does without network access."""
    calls = []

    def fail(name):
        calls.append(name)
        raise tslp.DownloadError(f"Failed to fetch manifest for {name}: Connection refused")

    get_pack_parser.cache_clear()
    monkeypatch.setattr(grammars.tslp, "get_parser", fail)
    yield calls
    get_pack_parser.cache_clear()


def test_download_failure_gives_a_clear_error(offline):
    with pytest.raises(GrammarUnavailableError) as info:
        get_pack_parser("go")
    message = str(info.value)
    assert info.value.grammar == "go"
    assert "'go'" in message
    assert "network access" in message
    assert "Connection refused" in message
    assert "t.download([" in message
    assert isinstance(info.value.__cause__, tslp.DownloadError)


def test_only_the_grammars_of_present_languages_load(offline, monkeypatch):
    monkeypatch.setattr(grammars.tslp, "get_parser", lambda name: offline.append(name))
    ensure_pack_grammars(["python", "vue", "java", "vue"])
    assert sorted(offline) == ["javascript", "typescript", "vue"]


def test_languages_without_pack_grammars_need_no_network(offline):
    ensure_pack_grammars(["python", "java", "rust"])
    assert offline == []


def test_analysis_stops_before_the_first_file(offline, tmp_path, monkeypatch):
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    analyzed = []
    monkeypatch.setattr(
        CallGraphAnalyzer, "_analyze_code_file", lambda self, base, info: analyzed.append(info)
    )
    with pytest.raises(GrammarUnavailableError, match="'go'"):
        CallGraphAnalyzer().analyze_code_files(
            [{"path": "main.go", "language": "go"}], str(tmp_path)
        )
    assert analyzed == []


def test_dockerfile_downloads_every_pack_grammar():
    match = re.search(r"t\.download\(\[([^\]]*)\]\)", DOCKERFILE.read_text(encoding="utf-8"))
    assert match, "docker/Dockerfile must download the pack grammars at build time"
    in_dockerfile = sorted(re.findall(r"'([^']+)'", match.group(1)))
    assert in_dockerfile == list(PACK_GRAMMARS)
