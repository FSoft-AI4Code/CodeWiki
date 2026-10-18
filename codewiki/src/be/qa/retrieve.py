"""Scouting and budgeted reading over a :class:`QAIndex`.

Scouting returns *metadata* only (id, kind, title or signature, location,
token cost) so a model can decide what is worth reading before any body is
loaded (FastCode's scouting-first idea). ``read`` / ``assemble`` then load
exactly the chosen sections and component bodies, never whole files, within
a token budget.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from codewiki.src.be.qa.index import QAIndex, approx_tokens

STALE_FACTOR = 0.5
GRAPH_FACTOR = 0.5
# FastCode eq. 2: priority = w1 * relevance + w2 * found_by_tool + w3 * density
W_REL, W_TOOL, W_DENSITY = 1.0, 0.6, 0.3
_GREP_MAX_FILES = 40
_GREP_MAX_HITS = 120
_GREP_MAX_BYTES = 1_000_000
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox"}


@dataclass
class Candidate:
    id: str
    score: float
    via: set[str] = field(default_factory=set)  # bm25 | grep | graph
    hits: list[str] = field(default_factory=list)  # grep lines that found it


@dataclass
class ReadItem:
    id: str
    kind: str
    title: str
    location: str
    text: str
    tokens: int
    stale: bool = False
    truncated: bool = False


class Retriever:
    def __init__(self, index: QAIndex) -> None:
        self.index = index

    # -- describing units ----------------------------------------------

    def describe(self, uid: str) -> dict:
        ix = self.index
        if uid in ix.sections:
            sec = ix.sections[uid]
            return {
                "id": uid,
                "kind": "section",
                "title": f"{sec.page_title} › {sec.title}" if sec.level > 1 else sec.title,
                "location": f"{sec.file}#{sec.anchor}",
                "tokens": sec.tokens,
                "stale": sec.stale,
                "snippet": sec.plain[:160],
            }
        unit = ix.code[uid]
        return {
            "id": uid,
            "kind": unit.kind,
            "title": unit.signature,
            "location": unit.location,
            "tokens": unit.tokens,
            "stale": False,
            "snippet": (unit.docstring.splitlines() or [""])[0][:160],
        }

    def line(self, cand: Candidate) -> str:
        """One compact line per candidate, for prompts and MCP results."""
        d = self.describe(cand.id)
        stale = " [stale doc]" if d["stale"] else ""
        via = "+".join(sorted(cand.via)) if cand.via else ""
        snippet = f" — {d['snippet']}" if d["snippet"] else ""
        return (
            f"{d['id']} · {d['kind']} · {d['title']} · ~{d['tokens']} tok"
            f"{stale}{' · ' + via if via else ''}{snippet}"
        )

    # -- scouting --------------------------------------------------------

    def search(
        self,
        query: str,
        keywords: list[str] | None = None,
        k: int = 15,
        kinds: set[str] | None = None,
    ) -> list[Candidate]:
        ix = self.index
        allowed = ix.doc_numbers(kinds) if kinds else None
        found: dict[str, Candidate] = {}
        for text, weight in ((query, 1.0), (" ".join(keywords or []), 0.8)):
            if not text.strip():
                continue
            # Rank everything: the stale discount below can reorder past any cut.
            for doc, score in ix.bm25.search(text, k=None, allowed=allowed):
                uid = ix.unit_id(doc)
                if uid in ix.sections and ix.sections[uid].stale:
                    score *= STALE_FACTOR
                cand = found.setdefault(uid, Candidate(uid, 0.0))
                cand.score = max(cand.score, score * weight)
                cand.via.add("bm25")
        ranked = sorted(found.values(), key=lambda c: c.score, reverse=True)
        return ranked[:k]

    def grep(self, patterns: list[str], max_files: int = _GREP_MAX_FILES) -> list[Candidate]:
        """Regex (or literal) search over the live checkout, mapped to components."""
        repo = self.index.repo_path
        if not repo or not patterns:
            return []
        compiled = []
        for pat in patterns[:8]:
            try:
                compiled.append(re.compile(pat))
            except re.error:
                compiled.append(re.compile(re.escape(pat)))
        found: dict[str, Candidate] = {}
        files_hit = 0
        hits = 0
        for rel in self._repo_files():
            if files_hit >= max_files or hits >= _GREP_MAX_HITS:
                break
            path = os.path.join(repo, rel)
            try:
                if os.path.getsize(path) > _GREP_MAX_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            file_had_hit = False
            for no, text in enumerate(lines, 1):
                if not any(rx.search(text) for rx in compiled):
                    continue
                file_had_hit = True
                hits += 1
                unit = self.index.component_at(rel, no)
                uid = unit.id if unit else None
                if uid is None:
                    continue
                cand = found.setdefault(uid, Candidate(uid, 0.0))
                cand.score += 1.0
                cand.via.add("grep")
                if len(cand.hits) < 3:
                    cand.hits.append(f"{rel}:{no}: {text.strip()[:120]}")
                if hits >= _GREP_MAX_HITS:
                    break
            files_hit += file_had_hit
        return sorted(found.values(), key=lambda c: c.score, reverse=True)

    def _repo_files(self):
        repo = self.index.repo_path
        docs = os.path.abspath(self.index.docs_dir)
        known = sorted({u.file for u in self.index.code.values()})
        seen = set(known)
        yield from known
        for root, dirs, files in os.walk(repo):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in _SKIP_DIRS
                and not d.startswith(".")
                and os.path.abspath(os.path.join(root, d)) != docs
            )
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(root, name), repo).replace(os.sep, "/")
                if rel not in seen:
                    yield rel

    def expand(self, cands: list[Candidate], hops: int = 1, limit: int = 30) -> list[Candidate]:
        """Add graph neighbours (calls, callers, page<->component) at a discount."""
        by_id = {c.id: c for c in cands}
        frontier = list(cands)
        for _ in range(hops):
            nxt = []
            for cand in frontier[:10]:
                for nid in self.index.neighbours(cand.id):
                    if nid in by_id:
                        continue
                    new = Candidate(nid, cand.score * GRAPH_FACTOR, {"graph"})
                    by_id[nid] = new
                    nxt.append(new)
            frontier = nxt
        ranked = sorted(by_id.values(), key=lambda c: c.score, reverse=True)
        return ranked[:limit]

    @staticmethod
    def merge(*lists: list[Candidate]) -> list[Candidate]:
        out: dict[str, Candidate] = {}
        for cands in lists:
            for c in cands:
                if c.id in out:
                    cur = out[c.id]
                    cur.score = max(cur.score, c.score)
                    cur.via |= c.via
                    cur.hits = (cur.hits + c.hits)[:3]
                else:
                    out[c.id] = Candidate(c.id, c.score, set(c.via), list(c.hits))
        return list(out.values())

    def priority(self, cands: list[Candidate]) -> list[Candidate]:
        """Order by relevance, tool evidence and density (small units first on ties)."""
        if not cands:
            return []
        top = max(c.score for c in cands) or 1.0

        def key(c: Candidate) -> float:
            tokens = self.describe(c.id)["tokens"]
            density = 1.0 / (1.0 + tokens / 800)
            return W_REL * c.score / top + W_TOOL * ("grep" in c.via) + W_DENSITY * density

        return sorted(cands, key=key, reverse=True)

    # -- reading ---------------------------------------------------------

    def read_one(self, uid: str, max_tokens: int | None = None) -> ReadItem | None:
        ix = self.index
        if uid in ix.sections:
            sec = ix.sections[uid]
            text, kind, location, title = sec.raw, "section", f"{sec.file}#{sec.anchor}", sec.title
            stale = sec.stale
        elif uid in ix.code:
            unit = ix.code[uid]
            text = unit.source
            kind, location, title, stale = unit.kind, unit.location, unit.signature, False
        else:
            return None
        truncated = False
        if max_tokens is not None and approx_tokens(text) > max_tokens:
            text = text[: int(max_tokens * 3.6)] + "\n… (truncated)"
            truncated = True
        return ReadItem(uid, kind, title, location, text, approx_tokens(text), stale, truncated)

    def _contained(self, ids: list[str]) -> set[str]:
        """Selected code units whose lines lie inside another selected unit of the same file.

        Reading ``Class`` and ``Class.method`` would send the method body twice;
        the container already holds it.
        """
        code = [self.index.code[i] for i in ids if i in self.index.code]
        inside = set()
        for unit in code:
            for other in code:
                if (
                    other.id != unit.id
                    and other.file == unit.file
                    and other.start_line <= unit.start_line
                    and unit.end_line <= other.end_line
                    and (other.end_line - other.start_line) > (unit.end_line - unit.start_line)
                ):
                    inside.add(unit.id)
                    break
        return inside

    def assemble(self, ids: list[str], budget_tokens: int) -> list[ReadItem]:
        """Greedy fill in the given order; a too-big first item is truncated, later ones skipped."""
        out: list[ReadItem] = []
        used = 0
        skip = self._contained(ids)
        for uid in ids:
            if uid in skip:
                continue
            item = self.read_one(uid)
            if item is None:
                continue
            if used + item.tokens > budget_tokens:
                if out:
                    continue
                item = self.read_one(uid, max_tokens=budget_tokens)
            out.append(item)
            used += item.tokens
            if used >= budget_tokens:
                break
        return out
