"""`codewiki serve`: the local Q&A API and the docs site's chat window."""

import json

import pytest
from fastapi.testclient import TestClient

from codewiki.src.be.qa.engine import QAEngine
from codewiki.src.be.qa.index import QAIndex
from codewiki.src.be.qa.server import create_app, origin_of
from codewiki.src.be.qa.sessions import SessionStore
from tests.qa_fixture import build
from tests.test_qa_engine import UNSIGN, ScriptedBackend

PAGES = "https://acme.github.io"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEWIKI_QA_LOG_DIR", str(tmp_path / "logs"))
    _, docs_dir = build(tmp_path)
    (docs_dir / "index.html").write_text("<html>docs</html>")
    backend = ScriptedBackend({"keep": [UNSIGN], "confidence": 95})

    def factory():
        return QAEngine(QAIndex(str(docs_dir)), backend, sessions=SessionStore())

    app = create_app(str(docs_dir), {"http://127.0.0.1:8765", PAGES}, engine_factory=factory)
    return TestClient(app), backend


def _events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_health_reports_freshness(client):
    c, _ = client
    data = c.get("/api/health").json()
    assert data["ok"] and data["repo"] == "repo"
    assert "up to date" in data["freshness"]["summary"]
    assert data["freshness"]["stale_pages"] == []


def test_static_site_served_but_not_internals(client):
    c, _ = client
    assert c.get("/").text == "<html>docs</html>"
    assert "# Timed" in c.get("/Timed.md").text
    assert c.get("/temp/dependency_graphs/repo_dependency_graph.json").status_code == 404
    assert c.get("/metadata.json").status_code == 200
    assert c.get("/../pkg/signer.py").status_code == 404
    assert c.get("/%2e%2e/pkg/signer.py").status_code == 404


def test_search_endpoint(client):
    c, _ = client
    results = c.post("/api/search", json={"query": "unsign expiry", "k": 5}).json()["results"]
    assert any(r["id"] == UNSIGN for r in results)
    assert c.post("/api/search", json={"query": "x", "kinds": "bogus"}).status_code == 400


def test_ask_streams_status_then_answer(client):
    c, backend = client
    response = c.post("/api/ask", json={"question": "How does expiry work?"})
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = _events(response)
    assert events[0]["type"] == "status"
    answer = events[-1]
    assert answer["type"] == "answer" and "It checks the age." in answer["answer"]
    assert answer["sources"][0]["id"] == UNSIGN
    assert answer["stats"]["llm_calls"] == 2 and "Answered in" in answer["stats"]["footer"]
    # follow-up keeps the session
    events = _events(
        c.post("/api/ask", json={"question": "And?", "session_id": answer["session_id"]})
    )
    assert events[-1]["session_id"] == answer["session_id"]
    assert c.post("/api/ask", json={"question": "  "}).status_code == 400


def test_ask_errors_are_streamed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEWIKI_QA_LOG_DIR", str(tmp_path / "logs"))
    _, docs_dir = build(tmp_path)

    class Broken:
        def ask(self, *a, **k):
            raise RuntimeError("usage limit reached")

    c = TestClient(create_app(str(docs_dir), set(), engine_factory=Broken))
    events = _events(c.post("/api/ask", json={"question": "q"}))
    assert events == [{"type": "error", "message": "usage limit reached"}]

    def no_config():
        raise RuntimeError("CodeWiki is not configured")

    c = TestClient(create_app(str(docs_dir), set(), engine_factory=no_config))
    r = c.post("/api/ask", json={"question": "q"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_only_allowed_origins_may_use_the_api(client):
    c, _ = client
    ok = c.get("/api/health", headers={"Origin": PAGES})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == PAGES
    preflight = c.options(
        "/api/ask",
        headers={
            "Origin": PAGES,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-private-network"] == "true"
    evil = c.post("/api/ask", headers={"Origin": "https://evil.example"}, json={"question": "q"})
    assert evil.status_code == 403
    assert "access-control-allow-origin" not in evil.headers
    # a page fetching a doc file cross-origin gets no CORS grant either
    page = c.get("/Timed.md", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in page.headers


def test_origin_of():
    assert origin_of("https://acme.github.io/repo/") == "https://acme.github.io"
    assert origin_of("not a url") is None


def test_generated_site_has_the_chat_window(tmp_path):
    from codewiki.cli.html_generator import HTMLGenerator

    out = tmp_path / "index.html"
    HTMLGenerator().generate(
        out,
        "demo",
        module_tree={},
        repository_url="https://github.com/acme/repo",
        metadata={"generation_info": {"commit_id": "abc123"}},
    )
    html = out.read_text()
    assert 'id="chat-trigger"' in html and 'id="chat-panel"' in html
    assert "dompurify@3.2.4" in html and "DOMPurify.sanitize" in html
    config = html.split("const CONFIG = ", 1)[1].split(";\n", 1)[0]
    data = json.loads(config)
    assert data["chat"]["endpoints"] == ["http://127.0.0.1:8765"]
    assert data["repository_url"] == "https://github.com/acme/repo"
    assert data["commit_id"] == "abc123"
