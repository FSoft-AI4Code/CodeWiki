"""The per-file parse timeout must not be swallowed by an analyzer's own
``except Exception`` blocks."""

import signal
import time

import pytest

from codewiki.src.be.dependency_analyzer.analysis import call_graph_analyzer
from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import (
    CallGraphAnalyzer,
)

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="the timeout relies on SIGALRM"
)


def _swallowing_analyzer(self, file_path, content, repo_dir):
    # Mimics a language analyzer that guards each small step with
    # ``except Exception`` and loops for a very long time.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            time.sleep(0.01)
        except Exception:  # noqa: BLE001
            pass


def test_timeout_escapes_except_exception(tmp_path, monkeypatch):
    (tmp_path / "slow.js").write_text("f();\n", encoding="utf-8")
    monkeypatch.setattr(CallGraphAnalyzer, "_analyze_javascript_file", _swallowing_analyzer)

    real_timeout = call_graph_analyzer.timeout
    monkeypatch.setattr(call_graph_analyzer, "timeout", lambda seconds: real_timeout(1))

    started = time.monotonic()
    CallGraphAnalyzer()._analyze_code_file(
        str(tmp_path), {"path": "slow.js", "language": "javascript"}
    )
    assert time.monotonic() - started < 10
