"""Parse the JSON verdict block an editing agent returns."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _candidates(text: str) -> list[str]:
    out = [m.group(1) for m in _FENCE_RE.finditer(text or "")]
    # Fallback: the last top-level {...} that mentions "verdicts" or "decisions".
    for key in ("verdicts", "decisions"):
        idx = (text or "").rfind(f'"{key}"')
        if idx == -1:
            continue
        start = (text or "").rfind("{", 0, idx)
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start : i + 1])
                    break
    return out


def parse_json_block(text: str) -> dict[str, Any] | None:
    for cand in reversed(_candidates(text)):
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_verdicts(text: str) -> tuple[dict[str, dict[str, str]], str]:
    """Return ``{page_stem: {"verdict": ..., "reason": ...}}`` and free-text notes."""
    data = parse_json_block(text) or {}
    raw = data.get("verdicts") or {}
    verdicts: dict[str, dict[str, str]] = {}
    if isinstance(raw, dict):
        for page, v in raw.items():
            stem = str(page)
            if stem.endswith(".md"):
                stem = stem[:-3]
            if isinstance(v, str):
                verdicts[stem] = {"verdict": v.strip().lower(), "reason": ""}
            elif isinstance(v, dict):
                verdicts[stem] = {
                    "verdict": str(v.get("verdict", "")).strip().lower(),
                    "reason": str(v.get("reason", "")).strip(),
                }
    elif isinstance(raw, list):
        for v in raw:
            if isinstance(v, dict) and "page" in v:
                stem = str(v["page"])
                stem = stem[:-3] if stem.endswith(".md") else stem
                verdicts[stem] = {
                    "verdict": str(v.get("verdict", "")).strip().lower(),
                    "reason": str(v.get("reason", "")).strip(),
                }
    notes = str(data.get("notes", "") or "")
    return verdicts, notes
