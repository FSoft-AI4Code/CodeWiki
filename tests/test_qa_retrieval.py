"""Q&A index, freshness, retrieval and the no-LLM MCP tools."""

import os
import time

import pytest

from codewiki.mcp.tools import qa as qa_tools
from codewiki.src.be.qa import DocsNotFoundError, get_index, resolve_docs_dir
from codewiki.src.be.qa.bm25 import BM25F, tokenize
from codewiki.src.be.qa.index import QAIndex
from codewiki.src.be.qa.retrieve import Retriever
from qa_fixture import build, git


@pytest.fixture
def docs(tmp_path):
    return build(tmp_path)


def test_tokenize_splits_identifiers_and_stems():
    terms = tokenize("TimestampSigner.unsign max_age timestamps")
    assert {"timestampsigner", "timestamp", "signer", "unsign", "max_age", "max", "age"} <= set(
        terms
    )
    assert "timestamps" not in terms  # stemmed to "timestamp"


def test_bm25f_prefers_title_matches_and_expands_prefixes():
    bm = BM25F({"title": 4.0, "content": 1.0})
    bm.add({"title": "unsign", "content": "verify a signature"})
    bm.add({"title": "sign", "content": "unsign is the reverse"})
    bm.finalize()
    assert bm.search("unsign")[0][0] == 0
    assert bm.search("signa")  # prefix of "signature"


def test_index_builds_sections_code_and_edges(docs):
    repo, docs_dir = docs
    ix = QAIndex(str(docs_dir))
    assert ix.repo_path == str(repo)
    assert "Signing.md#how-unsign-verifies-a-value" in ix.sections
    assert "pkg/timed.py::TimestampSigner.unsign" in ix.code
    unsign = ix.code["pkg/timed.py::TimestampSigner.unsign"]
    assert unsign.signature.startswith("def unsign(self, signed_value")
    assert unsign.start_line > 0 and unsign.end_line >= unsign.start_line
    # the page documents its components and the component links back to the page
    assert "pkg/timed.py::TimestampSigner.unsign" in ix.page_components["Timed.md"]
    assert any(n.startswith("Timed.md#") for n in ix.neighbours(unsign.id))
    assert ix.freshness.method == "git" and not ix.freshness.touched
    assert "Signing (Signing.md)" in ix.outline


def test_search_finds_docs_and_code(docs):
    _, docs_dir = docs
    r = Retriever(QAIndex(str(docs_dir)))
    ids = [c.id for c in r.search("how does expiry work when unsigning", k=8)]
    assert "pkg/timed.py::TimestampSigner.unsign" in ids
    assert any(i.startswith("Timed.md#") for i in ids)
    code_only = r.search("unsign", kinds={"code"})
    assert code_only and all("::" in c.id for c in code_only)


def test_grep_maps_hits_to_enclosing_components(docs):
    _, docs_dir = docs
    r = Retriever(QAIndex(str(docs_dir)))
    found = {c.id: c for c in r.grep([r"max_age"])}
    assert "pkg/timed.py::TimestampSigner.unsign" in found
    assert found["pkg/timed.py::TimestampSigner.unsign"].hits


def test_expand_adds_callers_callees_and_pages(docs):
    _, docs_dir = docs
    r = Retriever(QAIndex(str(docs_dir)))
    seed = r.search("TimestampSigner unsign", k=1, kinds={"code"})
    expanded = {c.id for c in r.expand(seed)}
    assert len(expanded) > 1
    assert any(i.startswith("Timed.md#") for i in expanded)


def test_assemble_never_exceeds_the_budget(docs):
    _, docs_dir = docs
    ix = QAIndex(str(docs_dir))
    r = Retriever(ix)
    ids = list(ix.code)[:6] + list(ix.sections)[:6]
    for budget in (50, 300, 2000):
        items = r.assemble(ids, budget)
        assert items and sum(i.tokens for i in items) <= budget + 10


def _score(retriever, query, uid):
    return next(c.score for c in retriever.search(query, k=50) if c.id == uid)


def test_freshness_overlay_and_stale_pages(docs):
    repo, docs_dir = docs
    timed = repo / "pkg" / "timed.py"
    timed.write_text(
        timed.read_text().replace(
            "    def get_timestamp(self):",
            "    def now(self):\n        return time.time()\n\n    def get_timestamp(self):",
        )
    )
    (repo / "pkg" / "signer.py").unlink()
    (repo / "pkg" / "extra.py").write_text("def helper():\n    return 1\n")

    ix = QAIndex(str(docs_dir))
    assert ix.freshness.changed >= {"pkg/timed.py", "pkg/extra.py"}
    assert ix.freshness.deleted == {"pkg/signer.py"}
    assert "pkg/timed.py::TimestampSigner.now" in ix.code  # live parse of the changed file
    assert ix.code["pkg/timed.py::TimestampSigner.now"].live
    assert "pkg/extra.py::helper" in ix.code  # new file, no page yet
    assert not any(c.startswith("pkg/signer.py") for c in ix.code)  # deleted file gone
    assert {"Timed.md", "Signing.md"} <= ix.stale_pages
    assert ix.sections["Timed.md#expiry-check"].stale
    # a stale section scores half its BM25 relevance
    uid = "Timed.md#expiry-check"
    raw = dict((ix.unit_id(d), s) for d, s in ix.bm25.search("expiry check", k=50))[uid]
    assert _score(Retriever(ix), "expiry check", uid) == pytest.approx(raw * 0.5)
    status = qa_tools.handle_status({"docs_dir": str(docs_dir)})
    assert "Stale pages" in status and "codewiki generate --update" in status


def test_freshness_counts_commits_behind(docs):
    repo, docs_dir = docs
    (repo / "pkg" / "timed.py").write_text((repo / "pkg" / "timed.py").read_text() + "\n# note\n")
    git(repo, "commit", "-qam", "tweak")
    ix = QAIndex(str(docs_dir))
    assert ix.freshness.commits_behind == 2  # docs commit + tweak after the recorded commit
    assert "pkg/timed.py" in ix.freshness.changed


def test_freshness_mtime_fallback_without_git(tmp_path):
    repo, docs_dir = build(tmp_path, use_git=False)
    time.sleep(0.01)
    later = time.time() + 5
    os.utime(repo / "pkg" / "timed.py", (later, later))
    ix = QAIndex(str(docs_dir))
    assert ix.freshness.method == "mtime"
    assert ix.freshness.changed == {"pkg/timed.py"}
    assert "Timed.md" in ix.stale_pages


def test_resolve_docs_dir_and_cache(docs, monkeypatch):
    repo, docs_dir = docs
    monkeypatch.chdir(repo)
    assert resolve_docs_dir() == str(docs_dir)
    monkeypatch.setenv("CODEWIKI_DOCS_DIR", str(docs_dir))
    assert get_index() is get_index(str(docs_dir))
    with pytest.raises(DocsNotFoundError):
        resolve_docs_dir(str(repo / "pkg"))


def test_mcp_search_read_outline(docs):
    _, docs_dir = docs
    d = str(docs_dir)
    out = qa_tools.handle_search({"docs_dir": d, "query": "unsign expiry", "k": 5})
    assert "pkg/timed.py::TimestampSigner.unsign" in out and "~" in out and "tok" in out
    read = qa_tools.handle_read(
        {
            "docs_dir": d,
            "ids": ["pkg/timed.py::TimestampSigner.unsign", "nope::x"],
            "max_tokens": 800,
        }
    )
    assert "def unsign(self, signed_value, max_age=MAX_AGE)" in read
    assert "class TimestampSigner" not in read  # the method body only, not the file
    assert "Unknown ids: nope::x" in read and "Related ids:" in read
    outline = qa_tools.handle_outline({"docs_dir": d})
    assert "Timed (Timed.md)" in outline and "up to date" in outline


def test_mcp_server_lists_and_routes_qa_tools(docs):
    import asyncio
    import json

    from codewiki.mcp import server

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"codewiki_search", "codewiki_read", "codewiki_outline", "codewiki_status"} <= names
    _, docs_dir = docs
    out = asyncio.run(
        server.call_tool("codewiki_search", {"query": "sign", "docs_dir": str(docs_dir)})
    )
    assert "pkg/signer.py" in out[0].text
    err = asyncio.run(server.call_tool("codewiki_read", {"ids": [], "docs_dir": str(docs_dir)}))
    assert "error" in json.loads(err[0].text)
