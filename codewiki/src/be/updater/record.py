"""The update record: every decision of one incremental step, written to
``update_record.json`` in the docs dir and summarised in ``metadata.json``."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

RECORD_FILENAME = "update_record.json"

OUTCOME_NO_CHANGE = "no_change"
OUTCOME_INCREMENTAL = "incremental"
OUTCOME_FULL_FALLBACK = "full_fallback"
OUTCOME_DETECTOR_FAILURE = "detector_failure"


@dataclass
class CallCost:
    kind: str  # leaf_agent | rewrite | routing | stale_fix | missing_page | recluster
    target: str
    seconds: float
    usage: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class PageVerdict:
    page: str
    verdict: str  # no-op | patch | rewrite | create | delete
    reason: str = ""
    by_leaf: str = ""
    changed_on_disk: bool | None = None


@dataclass
class UpdateRecord:
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    finished_at: str | None = None
    outcome: str = OUTCOME_INCREMENTAL
    options: dict[str, Any] = field(default_factory=dict)
    revision: dict[str, Any] = field(default_factory=dict)  # old/new commit, repo path
    diff: dict[str, Any] = field(default_factory=dict)
    repair: dict[str, Any] = field(default_factory=dict)
    reclustered: list[list[str]] = field(default_factory=list)
    reports: dict[str, Any] = field(default_factory=dict)
    # Step 3a: effective owners given to changed components no leaf tracks
    ownership: list[dict[str, Any]] = field(default_factory=list)
    active: list[dict[str, Any]] = field(default_factory=list)  # {leaf, mode, order}
    write_sets: dict[str, list[str]] = field(default_factory=dict)
    fallback: dict[str, Any] = field(default_factory=dict)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    pages_written: list[str] = field(default_factory=list)
    pages_removed: list[str] = field(default_factory=list)
    write_set_violations: list[dict[str, Any]] = field(default_factory=list)
    stale_scan: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    detector_notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wall_seconds: float | None = None

    def add_call(self, cost: CallCost) -> None:
        self.calls.append(asdict(cost))

    def add_verdict(self, v: PageVerdict) -> None:
        self.verdicts.append(asdict(v))

    def summary(self) -> dict[str, Any]:
        usage_total: dict[str, float] = {}
        for c in self.calls:
            for k, v in (c.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage_total[k] = usage_total.get(k, 0) + v
        return {
            "outcome": self.outcome,
            "finished_at": self.finished_at,
            "rung": self.options.get("rung"),
            "revision": self.revision,
            "diff_counts": self.diff.get("counts", {}),
            "n_active": len(self.active),
            "n_adopted": len(self.ownership),
            "n_adopted_by_agent": sum(
                1 for a in self.ownership if str(a.get("rule", "")).startswith("4:")
            ),
            "fallback": self.fallback,
            "n_calls": len(self.calls),
            "usage_total": usage_total,
            "pages_written": sorted(set(self.pages_written)),
            "pages_removed": sorted(set(self.pages_removed)),
            "wall_seconds": self.wall_seconds,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, docs_dir: str) -> str:
        path = os.path.join(docs_dir, RECORD_FILENAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        return path


def merge_into_metadata(docs_dir: str, summary: dict[str, Any]) -> None:
    """Append ``summary`` under ``last_update`` (and an ``update_history`` list)."""
    path = os.path.join(docs_dir, "metadata.json")
    meta: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            meta = {}
    meta["last_update"] = summary
    history = meta.get("update_history")
    if not isinstance(history, list):
        history = []
    history.append(summary)
    meta["update_history"] = history
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=4, ensure_ascii=False, default=str)
