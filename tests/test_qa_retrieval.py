"""Q&A index, freshness, retrieval and the no-LLM MCP tools."""

import os
import sys
import time

import pytest

from codewiki.mcp.tools import qa as qa_tools
from codewiki.src.be.qa import DocsNotFoundError, get_index, resolve_docs_dir
from codewiki.src.be.qa.bm25 import BM25F, stem, tokenize
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


def test_stem_meets_roots_ending_in_e_or_doubled_consonants():
    for family in (
        "merge merging merged merges",
        "save saved saving saves",
        "stop stopped stopping stops",
        "stem stemming stemmed stems",
        "call called calling calls",
        "add added adding",
        "class classes",
        "set setting settings",
    ):
        assert len(set(tokenize(family))) == 1, family
    assert stem("max_age") == "max_age" and stem("100") == "100"  # identifiers, numbers


def test_tokenize_keeps_the_word_of_private_names():
    assert "evict" in tokenize("SessionStore._evict")
    bm = BM25F({"title": 1.0})
    bm.add({"title": "_evict"})
    bm.finalize()
    assert bm.search("evicted")


def test_tokenize_keeps_non_ascii_words():
    assert "génération" in tokenize("Génération des pages")
    bm = BM25F({"content": 1.0})
    bm.add({"content": "Модуль конфигурации"})
    bm.add({"content": "другой текст"})
    bm.finalize()
    assert [doc for doc, _ in bm.search("конфигурации")] == [0]


def test_search_applies_the_stale_discount_before_cutting_to_k(docs):
    _, docs_dir = docs
    ix = QAIndex(str(docs_dir))
    stale = [sid for sid in ix.sections if sid.startswith("Timed.md#")][:2]
    for sid in stale:
        ix.sections[sid].stale = True
    code = "pkg/timed.py::TimestampSigner.unsign"
    doc_of = {uid: n for n, uid in enumerate(ix._unit_ids)}
    raw = [(doc_of[stale[0]], 10.0), (doc_of[stale[1]], 9.0), (doc_of[code], 8.0)]
    ix.bm25.search = lambda text, k=20, allowed=None: raw[:k]
    # halved, the stale sections (5.0, 4.5) rank below the code (8.0) at any k
    assert [c.id for c in Retriever(ix).search("q", k=1)] == [code]


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


def test_a_name_query_puts_its_definitions_first(docs):
    _, docs_dir = docs
    r = Retriever(QAIndex(str(docs_dir)))

    def top(query, k=4):
        return [c.id.split("::")[-1] for c in r.search(query, k=k)]

    assert top("TimestampSigner")[0] == "TimestampSigner"  # the class before its methods
    assert top("unsign")[:2] == ["Signer.unsign", "TimestampSigner.unsign"]
    assert top("Signer.unsign")[0] == "Signer.unsign"  # qualified: only that one is pinned
    assert top("signer.UNSIGN")[0] == "Signer.unsign"  # names match case-insensitively
    assert top("pkg/timed.py::TimestampSigner.unsign")[0] == "TimestampSigner.unsign"
    assert "name" in r.search("unsign", k=1)[0].via
    # Not a name: plain ranking, nothing pinned.
    assert all("name" not in c.via for c in r.search("how does expiry work", k=5))
    # Docs-only search never pins code.
    assert all(c.id in r.index.sections for c in r.search("unsign", kinds={"section"}))


def test_cut_keeps_the_part_that_matches_the_question():
    from codewiki.src.be.qa.retrieve import _cut

    lines = ["class Big:"] + [f"    def filler_{i}(self): return {i}" for i in range(200)]
    lines[150] = "    def rotate_segment(self): return self.wal.rotate()"
    text = "\n".join(lines)

    cut = _cut(text, 400, "how do we rotate the wal segment")
    assert cut.startswith("class Big:")  # the signature stays
    assert "def rotate_segment" in cut
    assert "line(s) skipped" in cut and len(cut) <= 400 + 60
    # Without a question, or with no matching line, the beginning is kept.
    assert _cut(text, 400, None).startswith("class Big:\n    def filler_0")
    assert "def rotate_segment" not in _cut(text, 400, "unrelated words")


def test_assemble_cuts_an_oversized_first_unit_around_the_focus(docs):
    _, docs_dir = docs
    r = Retriever(QAIndex(str(docs_dir)))
    uid = "pkg/timed.py::TimestampSigner"
    full = r.read_one(uid)
    budget = full.tokens // 3
    focused = r.assemble([uid], budget, focus="get_timestamp")[0]
    assert focused.truncated and focused.tokens <= budget + 20
    assert "def get_timestamp" in focused.text


def test_mcp_read_passes_the_query_on_as_the_focus(docs, monkeypatch):
    _, docs_dir = docs
    seen = {}
    real = Retriever.assemble

    def spy(self, ids, budget, focus=None):
        seen["focus"] = focus
        return real(self, ids, budget, focus=focus)

    monkeypatch.setattr(Retriever, "assemble", spy)
    qa_tools.handle_read(
        {"docs_dir": str(docs_dir), "ids": ["pkg/timed.py::TimestampSigner"], "query": "clock"}
    )
    assert seen["focus"] == "clock"


def test_repo_walk_survives_windows_device_names(docs, monkeypatch):
    """On Windows a stray file named NUL resolves to \\\\.\\nul and relpath raises
    "path is on mount '\\\\.\\nul', start on mount 'C:'"; grep and impact walk the
    repo for every Ask AI question."""
    from codewiki.src.be.qa.impact import _repo_files, find_references

    repo, docs_dir = docs
    # What `> nul` in Git Bash leaves behind. Windows needs \\?\ to create it
    # (a plain path writes to the NUL device) and then fails for real;
    # elsewhere relpath's Windows error is simulated.
    nul = os.path.join(os.path.abspath(repo), "nul")
    if sys.platform == "win32":
        nul = "\\\\?\\" + nul
    else:
        real = os.path.relpath

        def windows_relpath(path, start=os.curdir):
            if os.path.basename(path).lower() == "nul":
                raise ValueError("path is on mount '\\\\.\\nul', start on mount 'C:'")
            return real(path, start)

        monkeypatch.setattr(os.path, "relpath", windows_relpath)
    with open(nul, "w"):
        pass
    try:
        index = QAIndex(str(docs_dir))
        assert "nul" in set(_repo_files(index))
        assert Retriever(index).grep([r"\bmax_age\b"])
        assert find_references(index, "MAX_AGE").all_ids
    finally:
        os.remove(nul)  # pytest's own cleanup cannot delete it on Windows
