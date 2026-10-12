"""Per-answer statistics and the local query log."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

LOG_DIR_ENV = "CODEWIKI_QA_LOG_DIR"
LOG_DISABLE_ENV = "CODEWIKI_QA_LOG"  # "0" turns the query log off


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


@dataclass
class AskStats:
    seconds: float = 0.0
    llm_calls: int = 0
    rounds: int = 0
    stop_reason: str = ""
    intent: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    context_tokens: int = 0
    sources: int = 0
    stale_files: int = 0
    _started: float = field(default_factory=time.monotonic, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_usage(self, usage: dict | None) -> None:
        """Add one LLM call's usage (any provider's key names)."""
        self.llm_calls += 1
        if not usage:
            return
        cache_read = int(usage.get("cache_read_tokens") or 0)
        if "input_tokens" in usage:
            # Anthropic-style (claude-code): input_tokens excludes cached prompt
            # tokens, which are billed and reported separately.
            self.input_tokens += (
                int(usage.get("input_tokens") or 0)
                + cache_read
                + int(usage.get("cache_write_tokens") or 0)
            )
        else:
            # OpenAI-style: prompt_tokens already includes cached tokens.
            self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        self.cache_read_tokens += cache_read
        cost = usage.get("cost_usd")
        if cost:
            self.cost_usd = (self.cost_usd or 0.0) + float(cost)

    def finish(self) -> AskStats:
        self.seconds = round(time.monotonic() - self._started, 2)
        return self

    def footer(self) -> str:
        parts = [f"Answered in {self.seconds:.1f}s", f"{self.llm_calls} LLM call(s)"]
        if self.total_tokens:
            detail = f"{_k(self.input_tokens)} in / {_k(self.output_tokens)} out"
            if self.cache_read_tokens:
                detail += f", {_k(self.cache_read_tokens)} cached"
            parts.append(f"{_k(self.total_tokens)} tokens ({detail})")
        parts.append(f"{_k(self.context_tokens)} context")
        if self.cost_usd:
            parts.append(f"${self.cost_usd:.3f}")
        if self.stop_reason:
            parts.append(f"stopped: {self.stop_reason}")
        return " · ".join(parts)

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["total_tokens"] = self.total_tokens
        d["footer"] = self.footer()
        return d


def log_dir() -> str:
    return os.environ.get(LOG_DIR_ENV) or os.path.join(os.path.expanduser("~"), ".codewiki", "logs")


def log_question(
    stats: AskStats, question: str, docs_dir: str, log_questions: bool = True
) -> str | None:
    """Append one JSON line to ``~/.codewiki/logs/qa-YYYY-MM-DD.jsonl``.

    The log lives outside the docs directory, which is usually committed.
    Returns the log path, or None when logging is off or fails.
    """
    if os.environ.get(LOG_DISABLE_ENV) == "0":
        return None
    record = {"time": datetime.now().isoformat(timespec="seconds"), "docs_dir": docs_dir}
    if log_questions:
        record["question"] = question
    record.update(stats.to_dict())
    try:
        folder = log_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"qa-{datetime.now():%Y-%m-%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
    except OSError as e:
        logger.debug("Could not write the Q&A log: %s", e)
        return None
