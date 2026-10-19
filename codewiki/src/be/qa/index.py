"""The question-answering index over a generated docs directory.

Two kinds of unit, both searchable and readable by id:

* **sections** of the generated pages (one per heading, id ``page.md#anchor``),
  the natural-language layer the documentation agents wrote;
* **code components** from the dependency graph the generation saved
  (``temp/dependency_graphs/*.json``; id ``path/file.py::Name``), each with its
  signature, docstring, exact line range and source.

Edges link them: ``depends_on`` both ways between components, a module page
to the components it documents (``module_tree.json``) and to the components
it names (``temp/reference_index.json``).

The docs are a snapshot of one commit. Files changed since are re-parsed
(only those files) so code units always match the working tree, and pages
covering them are marked stale.
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from codewiki.src.be.crosslinker import _HEADING_RE, _iter_lines, _plain, github_slug
from codewiki.src.be.doc_layout import (
    METADATA_FILENAME,
    iter_tree_paths,
    list_doc_files,
    load_module_tree,
)
from codewiki.src.be.qa.bm25 import BM25F
from codewiki.src.be.qa.freshness import Freshness, detect, read_generation_info
from codewiki.src.be.search_index import parse_sections

logger = logging.getLogger(__name__)

FIELD_WEIGHTS = {"title": 4.0, "path": 2.0, "content": 1.0}
# Re-parsing more changed files than this at load time costs more than it
# helps; such docs are reported stale and code is read from the snapshot.
MAX_OVERLAY_FILES = 300
_CODE_INDEX_CHARS = 3000
_SENTENCE_RE = re.compile(r"(.+?[.!?])(\s|$)")


def approx_tokens(text: str) -> int:
    """Cheap token estimate (~3.6 characters per token for code and prose)."""
    return max(1, int(len(text or "") / 3.6) + 1)


@dataclass
class Section:
    id: str
    file: str
    anchor: str
    level: int
    title: str
    plain: str
    raw: str
    page_title: str
    stale: bool = False

    @property
    def tokens(self) -> int:
        return approx_tokens(self.raw)


@dataclass
class CodeUnit:
    id: str
    name: str
    kind: str
    file: str
    start_line: int
    end_line: int
    signature: str
    docstring: str
    source: str
    depends_on: set[str] = field(default_factory=set)
    live: bool = False  # re-parsed from the working tree (file changed since the docs)

    @property
    def tokens(self) -> int:
        return approx_tokens(self.source)

    @property
    def location(self) -> str:
        if self.start_line:
            return f"{self.file}:L{self.start_line}-L{self.end_line}"
        return self.file


def _signature(source: str, fallback: str) -> str:
    lines = []
    for line in (source or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("@", "#", "//", "/*", "*")):
            if lines:
                break
            continue
        lines.append(stripped)
        if stripped.endswith((":", "{", "=>", ";")) or len(lines) >= 3:
            break
    sig = " ".join(lines) or fallback
    return sig if len(sig) <= 240 else sig[:237] + "..."


def _raw_sections(text: str) -> dict[str, tuple[int, str]]:
    """Anchor -> (level, raw markdown of the section, heading line included).

    Uses the same heading detection and slugs as ``search_index.parse_sections``
    so ids line up with the viewer's search index.
    """
    lines = text.split("\n")
    heads: list[tuple[int, int, str]] = []
    seen: dict[str, int] = {}
    for idx, line, in_code in _iter_lines(text):
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        base = github_slug(_plain(m.group(2)).strip())
        count = seen.get(base, 0)
        seen[base] = count + 1
        heads.append((idx, len(m.group(1)), base if not count else f"{base}-{count}"))
    out: dict[str, tuple[int, str]] = {}
    for i, (start, level, slug) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(lines)
        out[slug] = (level, "\n".join(lines[start:end]).strip())
    return out


def _first_sentence(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    m = _SENTENCE_RE.match(text)
    sentence = m.group(1) if m else text
    return sentence if len(sentence) <= limit else sentence[: limit - 3] + "..."


def _resolve_repo(docs_dir: str, repo_path: str | None, graph_files: set[str]) -> str | None:
    candidates = []
    if repo_path:
        candidates.append(repo_path)
    recorded = read_generation_info(docs_dir).get("repo_path")
    if recorded:
        candidates.append(recorded)
    candidates.append(os.path.dirname(os.path.abspath(docs_dir)))
    sample = sorted(graph_files)[:20]
    for cand in candidates:
        if not cand or not os.path.isdir(cand):
            continue
        if not sample or any(os.path.exists(os.path.join(cand, rel)) for rel in sample):
            return os.path.abspath(cand)
    return None


def _overlay(repo_path: str, files: list[str]) -> dict[str, Any]:
    """Parse only ``files`` from the working tree; return their components."""
    from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

    parser = DependencyParser(repo_path, include_patterns=[glob.escape(f) for f in files])
    return parser.parse_repository()


class QAIndex:
    """Everything the Q&A engine searches and reads, built from a docs dir."""

    def __init__(self, docs_dir: str, repo_path: str | None = None) -> None:
        self.docs_dir = os.path.abspath(docs_dir)
        self.module_tree: dict[str, Any] = load_module_tree(self.docs_dir)
        self.sections: dict[str, Section] = {}
        self.code: dict[str, CodeUnit] = {}
        self.page_titles: dict[str, str] = {}
        self.page_components: dict[str, set[str]] = {}
        self.component_pages: dict[str, set[str]] = {}
        self.callers: dict[str, set[str]] = {}
        self.by_file: dict[str, list[CodeUnit]] = {}
        self.stale_pages: set[str] = set()
        self.page_sections: dict[str, list[str]] = {}
        self.outline: str = ""
        self._unit_ids: list[str] = []
        self.bm25 = BM25F(FIELD_WEIGHTS)

        graph = self._load_graph()
        self.repo_path = _resolve_repo(
            self.docs_dir, repo_path, {n.relative_path for n in graph.values() if n.relative_path}
        )
        logger.info("Checking what changed since the docs were generated…")
        self.freshness: Freshness = detect(
            self.docs_dir, self.repo_path, {n.relative_path for n in graph.values()}
        )
        self._build_code(graph)
        logger.info("Indexing doc pages and %d code component(s)…", len(self.code))
        self._build_pages()
        self._build_index()
        self.stamp = self.compute_stamp(self.docs_dir)

    # -- loading -------------------------------------------------------

    def _load_graph(self) -> dict[str, Any]:
        from codewiki.src.be.updater.graph_store import find_any_graph_file, load_graph

        path = find_any_graph_file(os.path.join(self.docs_dir, "temp", "dependency_graphs"))
        if not path:
            logger.warning("No dependency graph under %s/temp; code search is off", self.docs_dir)
            return {}
        try:
            return load_graph(path)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("Could not load dependency graph %s: %s", path, e)
            return {}

    def _build_code(self, graph: dict[str, Any]) -> None:
        live: dict[str, Any] = {}
        replaced: set[str] = set(self.freshness.deleted)
        to_parse = sorted(
            f
            for f in self.freshness.changed
            if self.repo_path and os.path.isfile(os.path.join(self.repo_path, f))
        )
        if to_parse and len(to_parse) <= MAX_OVERLAY_FILES:
            logger.info("Re-reading %d file(s) changed since the docs…", len(to_parse))
            try:
                live = _overlay(self.repo_path, to_parse)
                replaced |= set(to_parse)
            except Exception as e:  # noqa: BLE001 — fall back to the snapshot
                logger.warning("Could not re-parse changed files: %s", e)
        elif to_parse:
            logger.warning(
                "%d files changed since the docs; reading code from the snapshot", len(to_parse)
            )

        for cid, node in graph.items():
            if node.relative_path in replaced:
                continue  # replaced by the live parse, or the file is gone
            self.code[cid] = self._unit(cid, node, live=False)
        for cid, node in live.items():
            unit = self._unit(cid, node, live=True)
            old = graph.get(cid)
            if old is not None:  # the subset parse cannot resolve calls into other files
                unit.depends_on |= {d for d in old.depends_on if d in graph}
            self.code[cid] = unit
        for cid, unit in self.code.items():
            unit.depends_on = {d for d in unit.depends_on if d in self.code and d != cid}
            for dep in unit.depends_on:
                self.callers.setdefault(dep, set()).add(cid)
            self.by_file.setdefault(unit.file, []).append(unit)

    @staticmethod
    def _unit(cid: str, node: Any, live: bool) -> CodeUnit:
        source = node.source_code or ""
        return CodeUnit(
            id=cid,
            name=node.display_name or node.name,
            kind=node.component_type or node.node_type or "code",
            file=node.relative_path or cid.split("::", 1)[0],
            start_line=node.start_line or 0,
            end_line=node.end_line or 0,
            signature=_signature(source, node.name),
            docstring=(node.docstring or "").strip(),
            source=source,
            depends_on=set(node.depends_on or ()),
            live=live,
        )

    def _build_pages(self) -> None:
        pages = list_doc_files(self.docs_dir, self.module_tree)  # stem -> rel
        touched = self.freshness.touched

        def file_of(cid: str) -> str:
            # A deleted file's components are gone from self.code; its id still names it.
            unit = self.code.get(cid)
            return unit.file if unit else cid.split("::", 1)[0]

        # module page <-> the components it documents (descendants included)
        def walk(tree: dict[str, Any]) -> set[str]:
            covered: set[str] = set()
            for name, info in tree.items():
                if not isinstance(info, dict):
                    continue
                own = set(info.get("components") or [])
                below = walk(info.get("children") or {})
                rel = pages.get(name)
                if rel:
                    self.page_components.setdefault(rel, set()).update(
                        c for c in own if c in self.code
                    )
                    if any(file_of(c) in touched for c in own | below):
                        self.stale_pages.add(rel)
                covered |= own | below
            return covered

        walk(self.module_tree)

        from codewiki.src.be.updater.reference_index import load_reference_index

        refs = load_reference_index(self.docs_dir) or {}
        for stem, entry in refs.items():
            rel = pages.get(stem)
            if rel:
                self.page_components.setdefault(rel, set()).update(
                    c for c in entry.get("ids", []) if c in self.code
                )
        for rel, cids in self.page_components.items():
            for cid in cids:
                self.component_pages.setdefault(cid, set()).add(rel)

        for rel in sorted(set(pages.values())):
            try:
                with open(os.path.join(self.docs_dir, rel), encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            raw = _raw_sections(text)
            records = parse_sections(text)
            page_title = records[0].title if records else os.path.splitext(rel)[0]
            self.page_titles[rel] = page_title
            for rec in records:
                level, raw_text = raw.get(rec.anchor, (rec.level, rec.content))
                sid = f"{rel}#{rec.anchor}"
                self.sections[sid] = Section(
                    id=sid,
                    file=rel,
                    anchor=rec.anchor,
                    level=level,
                    title=rec.title,
                    plain=rec.content,
                    raw=raw_text,
                    page_title=page_title,
                    stale=rel in self.stale_pages,
                )
                self.page_sections.setdefault(rel, []).append(sid)

        self.outline = self._render_outline(pages)

    def _page_summary(self, rel: str) -> str:
        for sid in self.page_sections.get(rel, ()):
            if self.sections[sid].plain:
                return _first_sentence(self.sections[sid].plain)
        return ""

    def _render_outline(self, pages: dict[str, str]) -> str:
        lines = []
        if "overview" in pages:
            lines.append(f"overview.md — {self._page_summary(pages['overview'])}")
        for path in sorted(iter_tree_paths(self.module_tree)):
            rel = pages.get(path[-1])
            if not rel:
                continue
            stale = " [stale]" if rel in self.stale_pages else ""
            summary = self._page_summary(rel)
            lines.append(f"{'  ' * (len(path) - 1)}- {path[-1]} ({rel}){stale}: {summary}")
        return "\n".join(lines)

    def _build_index(self) -> None:
        for sec in self.sections.values():
            self._unit_ids.append(sec.id)
            self.bm25.add(
                {
                    "title": sec.title,
                    "path": f"{sec.file} {sec.page_title}",
                    "content": sec.plain,
                }
            )
        for unit in self.code.values():
            self._unit_ids.append(unit.id)
            self.bm25.add(
                {
                    "title": f"{unit.name} {unit.id.split('::')[-1]}",
                    "path": unit.file,
                    "content": " ".join(
                        (unit.signature, unit.docstring, unit.source[:_CODE_INDEX_CHARS])
                    ),
                }
            )
        self.bm25.finalize()

    # -- lookups -------------------------------------------------------

    def unit_id(self, doc: int) -> str:
        return self._unit_ids[doc]

    def doc_numbers(self, kinds: set[str]) -> set[int]:
        """BM25 doc numbers of the units of the given kinds ("section", "code")."""
        return {
            i
            for i, uid in enumerate(self._unit_ids)
            if ("section" if uid in self.sections else "code") in kinds
        }

    def kind_of(self, uid: str) -> str:
        if uid in self.sections:
            return "section"
        unit = self.code.get(uid)
        return unit.kind if unit else "unknown"

    def neighbours(self, uid: str) -> set[str]:
        if uid in self.sections:
            return set(self.page_components.get(self.sections[uid].file, ()))
        unit = self.code.get(uid)
        if unit is None:
            return set()
        out = set(unit.depends_on) | self.callers.get(uid, set())
        for rel in self.component_pages.get(uid, ()):
            out |= {sid for sid in self.page_sections.get(rel, ()) if self.sections[sid].level <= 2}
        return out

    def component_at(self, rel_file: str, line: int) -> CodeUnit | None:
        """The innermost component of ``rel_file`` that contains ``line``."""
        best = None
        for unit in self.by_file.get(rel_file, ()):
            if unit.start_line <= line <= unit.end_line:
                if best is None or (unit.end_line - unit.start_line) < (
                    best.end_line - best.start_line
                ):
                    best = unit
        return best

    @staticmethod
    def compute_stamp(docs_dir: str) -> tuple:
        """Changes when pages, the tree, the graph or the metadata are rewritten."""
        paths = [os.path.join(docs_dir, METADATA_FILENAME)]
        paths += glob.glob(os.path.join(docs_dir, "*.md"))
        paths += glob.glob(os.path.join(docs_dir, "**", "*.md"), recursive=True)
        paths += glob.glob(os.path.join(docs_dir, "temp", "dependency_graphs", "*.json"))
        stamp = []
        for p in sorted(set(paths)):
            try:
                stamp.append((p, os.path.getmtime(p)))
            except OSError:
                pass
        return tuple(stamp)
