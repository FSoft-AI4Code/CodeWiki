"""Prompts for the scouting rounds and the final answer.

Scouting prompts carry metadata only (outline, one line per candidate with
its token cost), never bodies; only the answer prompt gets content.
"""

from __future__ import annotations

PLAN_SYSTEM = (
    "You plan how to answer a developer's question about a codebase from its generated "
    "documentation and source code. You see only metadata (a module outline and candidate "
    "units with their token cost), not their content. Reply with one JSON object and nothing else."
)

PLAN_PROMPT = """Question: {question}
{history}
Documentation outline (module pages, one line each):
{outline}

Candidate units found by keyword search on the raw question ("id · kind · title/signature · ~tokens · how found — snippet"):
{candidates}

Repository freshness: {freshness}

Reply with JSON:
{{
  "intent": "explain" | "locate" | "debug" | "impact",
  "impact": {{"symbol": "<name, e.g. MAX_AGE or Class.method or Class.method.param>", "file": "<repo-relative file or empty>", "change": "rename" | "remove" | "modify"}} or null,
  "rewritten_query": "<the question restated with the codebase's own terms>",
  "keywords": ["<identifiers, API names and domain terms likely in the code or docs>"],
  "pseudocode": "<optional: a few lines of the code shape you expect to find>",
  "grep": ["<optional regexes for exact usages, e.g. \\\\bmax_age\\\\b>"],
  "complexity": <0-100, how much code must be read to answer>,
  "keep": ["<ids from the candidates that are clearly needed>"],
  "confidence": <0-100, how sure you are that the kept units are enough to answer>
}}
Use "impact" when the question asks what happens if something is changed, renamed or removed."""

ROUND_SYSTEM = (
    "You choose which parts of a codebase to read to answer a question, within a token budget. "
    "You see metadata only. Reply with one JSON object and nothing else."
)

ROUND_PROMPT = """Question: {question}

Candidates ("id · kind · title/signature · ~tokens · how found — snippet"; [kept] = currently selected):
{candidates}
{impact}
Budget: {used} of {budget} tokens selected. Round {round} of {max_rounds}.

Reply with JSON:
{{
  "keep": ["<the full set of ids to read; drop ones that are not needed>"],
  "search": ["<optional: new search queries for things still missing>"],
  "grep": ["<optional: regexes for exact usages still missing>"],
  "confidence": <0-100, how sure you are that the kept units are enough to answer>
}}"""

ANSWER_SYSTEM = (
    "You answer a developer's question about their codebase using only the documentation "
    "sections and source code provided. Be precise and concise. Cite every claim with the "
    "unit's location in backticks, e.g. `src/pkg/mod.py:L10-L42` or `Module.md#section`. "
    "Prefer the code over documentation marked stale. If the context does not contain the "
    "answer, say what is missing instead of guessing."
)

IMPACT_GUIDE = """This is a change-impact question. Structure the answer as:
1. Direct usages: each site and what the change would do there.
2. Affected callers further out.
3. Tests that need updating.
4. Risk: low / medium / high, with the reason.
5. Safe steps to make the change.
Mention dynamic access the static analysis cannot see, if listed."""

ANSWER_PROMPT = """Question: {question}
{history}{impact_guide}
{impact}
Context ({count} units, {tokens} tokens):

{context}

Freshness: {freshness}
{summary_request}"""

SUMMARY_REQUEST = """After the answer, add a block for follow-up questions:
<SUMMARY>
files and units read; key facts established; what is still unknown (under 150 words)
</SUMMARY>"""


def render_history(turns: list[dict]) -> str:
    if not turns:
        return ""
    lines = ["\nEarlier in this conversation:"]
    for turn in turns:
        lines.append(f"- Q: {turn['question']}\n  {turn['summary']}")
    return "\n".join(lines) + "\n"
