"""Answer a question from the generated docs + source code, cheaply.

FastCode's loop, on CodeWiki's index:

1. **Plan** (LLM, metadata only): intent, a rewritten query, keywords,
   pseudocode hints, grep patterns, complexity and a first selection.
2. **Scout**: BM25F on the rewrite + keywords (the rewrite is *used*;
   FastCode parses and drops it), grep over the live checkout, graph
   expansion, and the impact finder for "what breaks if…" questions.
3. **Refine** (LLM, metadata only, up to ``max_rounds``): keep / drop /
   ask for more, stopping when confident, out of leads, out of budget, or
   when two rounds in a row add little confidence per token read.
4. **Answer** (LLM): only the chosen sections and function/class bodies,
   within a budget that scales with the question's complexity.

A confident first plan goes straight to the answer: two LLM calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from codewiki.src.be.qa import prompts as P
from codewiki.src.be.qa.impact import ImpactReport, find_references
from codewiki.src.be.qa.index import QAIndex
from codewiki.src.be.qa.retrieve import Candidate, ReadItem, Retriever
from codewiki.src.be.qa.sessions import SESSIONS, SessionStore
from codewiki.src.be.qa.stats import AskStats, log_question

logger = logging.getLogger(__name__)

CONFIDENT = 85
MIN_GAIN_PER_1K = 2.0  # confidence points per 1k tokens added, below = a low-gain round
BASE_BUDGET = 12_000
MAX_BUDGET = 60_000
_OUTLINE_CHARS = 9_000
_SUMMARY_RE = re.compile(r"<SUMMARY>(.*?)</SUMMARY>", re.DOTALL | re.IGNORECASE)


@dataclass
class AskResult:
    answer: str
    sources: list[dict]
    stats: AskStats
    session_id: str
    impact: str | None = None
    freshness: str = ""

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "stats": self.stats.to_dict(),
            "session_id": self.session_id,
            "impact": self.impact,
            "freshness": self.freshness,
        }

    def render(self) -> str:
        """Answer + sources + stats as text (MCP results, the terminal)."""
        lines = [self.answer.strip(), ""]
        if self.sources:
            lines.append("Sources: " + ", ".join(s["location"] for s in self.sources))
        lines.append(f"[{self.stats.footer()} · {self.freshness} · session {self.session_id}]")
        return "\n".join(lines)


def parse_json(text: str | None) -> dict | None:
    """The first JSON object in an LLM reply (fences, prose and trailing commas tolerated)."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        chunk = text[m.start() :]
        for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
            try:
                obj, _ = decoder.raw_decode(candidate)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _int(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _strings(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def quiet_cli_output() -> None:
    """Stop caw printing session transcripts to stdout.

    The MCP server speaks JSON-RPC over stdout and ``codewiki serve`` streams
    answers; a transcript would corrupt one and clutter the other. CAW_LOG
    still opts back in.
    """
    if "CAW_LOG" in os.environ:
        return
    try:
        from codewiki._vendor.caw import set_global_display

        set_global_display(None)
    except ImportError:  # pragma: no cover — caw is vendored
        pass


@dataclass
class _Run:
    question: str
    stats: AskStats
    history: list[dict]
    on_event: Callable[[dict], None]
    cands: dict[str, Candidate] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)
    impact: ImpactReport | None = None


class QAEngine:
    def __init__(
        self,
        index: QAIndex,
        backend,
        *,
        max_rounds: int = 3,
        sessions: SessionStore = SESSIONS,
        log_questions: bool = True,
    ) -> None:
        self.index = index
        self.backend = backend
        self.retriever = Retriever(index)
        self.max_rounds = max(1, max_rounds)
        self.sessions = sessions
        self.log_questions = log_questions
        quiet_cli_output()

    # -- LLM -----------------------------------------------------------

    def _llm(self, run: _Run, prompt: str, system: str) -> str:
        try:
            reply = self.backend.complete(prompt, system_prompt=system, allow_tools=False)
        except TypeError:  # a backend without the allow_tools option
            reply = self.backend.complete(prompt, system_prompt=system)
        run.stats.add_usage(getattr(self.backend, "last_usage", None))
        return reply or ""

    # -- candidates ----------------------------------------------------

    def _add(self, run: _Run, cands: list[Candidate]) -> int:
        new = 0
        for c in cands:
            if c.id in run.cands:
                cur = run.cands[c.id]
                cur.score = max(cur.score, c.score)
                cur.via |= c.via
            else:
                run.cands[c.id] = c
                new += 1
        return new

    def _scout(self, run: _Run, queries: list[str], keywords: list[str], grep: list[str]) -> int:
        found: list[Candidate] = []
        for q in queries:
            if q.strip():
                found += self.retriever.search(q, keywords, k=15)
        found += self.retriever.grep(grep)
        merged = self.retriever.merge(found)
        return self._add(run, self.retriever.expand(merged, hops=1, limit=40))

    def _candidate_lines(self, run: _Run, limit: int = 40) -> str:
        ranked = self.retriever.priority(list(run.cands.values()))
        kept = set(run.kept)
        ordered = [c for c in ranked if c.id in kept] + [c for c in ranked if c.id not in kept]
        lines = []
        for c in ordered[:limit]:
            mark = "[kept] " if c.id in kept else ""
            lines.append(mark + self.retriever.line(c))
        return "\n".join(lines) or "(none)"

    def _valid_ids(self, run: _Run, value: Any, limit: int) -> list[str]:
        """Ids the model chose that exist in the index (any unit, not only shown ones)."""
        ids = []
        for uid in _strings(value, limit):
            if uid in self.index.sections or uid in self.index.code:
                run.cands.setdefault(uid, Candidate(uid, 0.0, {"model"}))
                ids.append(uid)
        return list(dict.fromkeys(ids))

    def _tokens(self, ids: list[str]) -> int:
        return sum(self.retriever.describe(i)["tokens"] for i in ids)

    def _budget(self, complexity: int, cap: int | None) -> int:
        repo_factor = min(2.0, max(0.5, 0.5 + len(self.index.code) / 2000))
        budget = int(BASE_BUDGET * (0.5 + complexity / 100) * repo_factor)
        budget = min(budget, MAX_BUDGET)
        return min(budget, cap) if cap else budget

    def _status(self, run: _Run, message: str) -> None:
        try:
            run.on_event({"type": "status", "message": message})
        except Exception:  # noqa: BLE001 — a broken listener must not break the answer
            logger.debug("status listener failed", exc_info=True)

    # -- the pipeline --------------------------------------------------

    def ask(
        self,
        question: str,
        session_id: str | None = None,
        max_context_tokens: int | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> AskResult:
        question = (question or "").strip()
        if not question:
            raise ValueError("question is required")
        session_id, history = self.sessions.get_or_create(session_id)
        run = _Run(question, AskStats(), history, on_event or (lambda _e: None))
        ix = self.index
        freshness = ix.freshness.summary()
        run.stats.stale_files = len(ix.freshness.touched)

        # 1. plan
        self._status(run, "Searching the docs and code…")
        self._add(run, self.retriever.search(question, k=15))
        plan = (
            parse_json(
                self._llm(
                    run,
                    P.PLAN_PROMPT.format(
                        question=question,
                        history=P.render_history(history),
                        outline=ix.outline[:_OUTLINE_CHARS] or "(no module pages)",
                        candidates=self._candidate_lines(run, 15),
                        freshness=freshness,
                    ),
                    P.PLAN_SYSTEM,
                )
            )
            or {}
        )
        run.stats.rounds = 1
        intent = str(plan.get("intent") or "explain")
        run.stats.intent = intent
        complexity = _int(plan.get("complexity"), 50)
        confidence = _int(plan.get("confidence"), 0)
        budget = self._budget(complexity, max_context_tokens)

        # 2. scout with the rewrite (FastCode computes it and then searches the raw question)
        queries = [str(plan.get("rewritten_query") or ""), str(plan.get("pseudocode") or "")]
        self._scout(run, queries, _strings(plan.get("keywords")), _strings(plan.get("grep"), 8))
        impact_spec = plan.get("impact") if isinstance(plan.get("impact"), dict) else None
        if intent == "impact" and impact_spec and impact_spec.get("symbol"):
            self._status(run, f"Finding every use of {impact_spec['symbol']}…")
            run.impact = find_references(
                ix,
                str(impact_spec["symbol"]),
                file=str(impact_spec.get("file") or "") or None,
                change=str(impact_spec.get("change") or "modify"),
            )
            top = max((c.score for c in run.cands.values()), default=1.0)
            self._add(
                run,
                [Candidate(cid, top, {"impact"}) for cid in run.impact.all_ids[:25]],
            )
        run.kept = self._valid_ids(run, plan.get("keep"), 30)

        # 3. refine
        stop = "confident" if confidence >= CONFIDENT and run.kept else ""
        low_gain = 0
        prev_conf, prev_tokens = confidence, self._tokens(run.kept)
        while not stop:
            if run.stats.rounds >= self.max_rounds:
                stop = "max rounds"
                break
            run.stats.rounds += 1
            self._status(run, f"Choosing what to read (round {run.stats.rounds})…")
            step = (
                parse_json(
                    self._llm(
                        run,
                        P.ROUND_PROMPT.format(
                            question=question,
                            candidates=self._candidate_lines(run),
                            impact=(
                                f"\nImpact scan:\n{run.impact.render(25)}\n" if run.impact else ""
                            ),
                            used=prev_tokens,
                            budget=budget,
                            round=run.stats.rounds,
                            max_rounds=self.max_rounds,
                        ),
                        P.ROUND_SYSTEM,
                    )
                )
                or {}
            )
            keep = self._valid_ids(run, step.get("keep"), 40)
            if keep:
                run.kept = keep
            confidence = _int(step.get("confidence"), prev_conf)
            more_search, more_grep = _strings(step.get("search"), 4), _strings(step.get("grep"), 6)
            tokens = self._tokens(run.kept)
            gain = (confidence - prev_conf) / max(1.0, (tokens - prev_tokens) / 1000)
            low_gain = low_gain + 1 if gain < MIN_GAIN_PER_1K else 0
            prev_conf, prev_tokens = confidence, tokens
            if confidence >= CONFIDENT:
                stop = "confident"
            elif tokens >= budget:
                stop = "budget"
            elif low_gain >= 2:
                stop = "low gain"
            elif not more_search and not more_grep:
                stop = "no more leads"
            else:
                self._scout(run, more_search, [], more_grep)
        run.stats.stop_reason = stop or "max rounds"

        # 4. answer
        ordered = run.kept or [c.id for c in self.retriever.priority(list(run.cands.values()))]
        items = self.retriever.assemble(ordered, budget)
        run.stats.context_tokens = sum(i.tokens for i in items)
        run.stats.sources = len(items)
        self._status(run, f"Reading {len(items)} item(s) and writing the answer…")
        reply = self._llm(
            run,
            P.ANSWER_PROMPT.format(
                question=question,
                history=P.render_history(history),
                impact_guide=("\n" + P.IMPACT_GUIDE + "\n") if run.impact else "",
                impact=f"\nImpact scan:\n{run.impact.render()}\n" if run.impact else "",
                count=len(items),
                tokens=run.stats.context_tokens,
                context=self._render_context(items),
                freshness=freshness,
                summary_request=P.SUMMARY_REQUEST,
            ),
            P.ANSWER_SYSTEM,
        )
        summary_match = _SUMMARY_RE.search(reply)
        answer = _SUMMARY_RE.sub("", reply).strip() or "(no answer)"
        if summary_match:
            self.sessions.add_turn(session_id, question, summary_match.group(1).strip())
        stale = sorted({i.location.split("#")[0] for i in items if i.stale})
        if stale:
            answer += (
                "\n\n_Note: the docs for "
                + ", ".join(stale)
                + " are older than the code; this answer relies on the current code._"
            )

        run.stats.finish()
        result = AskResult(
            answer=answer,
            sources=[self._source(i) for i in items],
            stats=run.stats,
            session_id=session_id,
            impact=run.impact.render() if run.impact else None,
            freshness=freshness,
        )
        log_question(run.stats, question, ix.docs_dir, self.log_questions)
        logger.debug("Q&A: %s", run.stats.footer())
        return result

    @staticmethod
    def _render_context(items: list[ReadItem]) -> str:
        blocks = []
        for item in items:
            stale = (
                " [stale: written for an older commit; the code changed since]"
                if item.stale
                else ""
            )
            if item.kind == "section":
                blocks.append(f"### {item.location}{stale}\n{item.text}")
            else:
                blocks.append(
                    f"### `{item.location}` ({item.kind} {item.id})\n```\n{item.text}\n```"
                )
        return "\n\n".join(blocks)

    @staticmethod
    def _source(item: ReadItem) -> dict:
        return {
            "id": item.id,
            "kind": item.kind,
            "title": item.title,
            "location": item.location,
            "stale": item.stale,
        }
