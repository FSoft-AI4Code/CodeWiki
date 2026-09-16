"""Step 1: component-level diff between two saved code graphs.

Components are joined on id (``path::name``). For each pair we compare a
whitespace-normalised hash of the body and the signature
``(name, parameters, base_classes)``. Deleted/added pairs whose bodies are
near-identical are paired as renames.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.updater.options import UpdateOptions

logger = logging.getLogger(__name__)

CLASS_ADDED = "added"
CLASS_DELETED = "deleted"
CLASS_IFACE = "interface"
CLASS_BODY = "body"
CLASS_EDGE = "edge"
CLASS_RENAMED = "renamed"

DIFF_TRUNCATED_MARKER = "... [diff truncated] ..."

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"\w+|[^\w\s]")


def normalize_body(text: str | None) -> str:
    return _WS.sub(" ", (text or "").strip())


def body_hash(node: Node) -> str:
    return hashlib.sha1(normalize_body(node.source_code).encode("utf-8")).hexdigest()


def signature(node: Node) -> dict[str, Any]:
    return {
        "name": node.name,
        "parameters": list(node.parameters or []),
        "base_classes": list(node.base_classes or []),
    }


def _tokens(text: str | None) -> list[str]:
    return _TOKEN.findall(text or "")


def body_similarity(a: str | None, b: str | None) -> float:
    """Token-level similarity in [0, 1] (``difflib`` ratio over token lists)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()


def _approx_tokens(text: str) -> int:
    # Cheap estimate; good enough to cap a diff. ~4 chars per token.
    return len(text) // 4 + 1


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or _approx_tokens(text) <= max_tokens:
        return text
    budget = max_tokens * 4
    head = text[: budget // 2]
    tail = text[-(budget // 2) :]
    return f"{head}\n{DIFF_TRUNCATED_MARKER}\n{tail}"


def unified_body_diff(old: Node | None, new: Node | None, label: str) -> str:
    old_lines = (old.source_code or "").splitlines(keepends=True) if old else []
    new_lines = (new.source_code or "").splitlines(keepends=True) if new else []
    diff = difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{label}", tofile=f"b/{label}", n=2
    )
    return "".join(diff)


@dataclass
class ChangeRecord:
    component_id: str  # new id (or old id for deletions)
    change_class: str
    old_id: str | None = None
    new_id: str | None = None
    relative_path: str | None = None
    component_type: str | None = None
    old_signature: dict[str, Any] | None = None
    new_signature: dict[str, Any] | None = None
    diff: str = ""
    similarity: float | None = None
    signature_changed: bool = False
    edges_added: list[str] = field(default_factory=list)
    edges_removed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphDiff:
    added: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    interface: set[str] = field(default_factory=set)
    body: set[str] = field(default_factory=set)
    edge: set[str] = field(default_factory=set)
    renamed: dict[str, str] = field(default_factory=dict)  # old id -> new id
    records: dict[str, ChangeRecord] = field(default_factory=dict)

    @property
    def changed_ids(self) -> set[str]:
        """Every id that appears in the change set (new ids for renames)."""
        return (
            set(self.added)
            | set(self.deleted)
            | set(self.interface)
            | set(self.body)
            | set(self.edge)
            | set(self.renamed.values())
        )

    @property
    def is_empty(self) -> bool:
        return not self.changed_ids

    def counts(self) -> dict[str, int]:
        return {
            CLASS_ADDED: len(self.added),
            CLASS_DELETED: len(self.deleted),
            CLASS_IFACE: len(self.interface),
            CLASS_BODY: len(self.body),
            CLASS_EDGE: len(self.edge),
            CLASS_RENAMED: len(self.renamed),
        }

    def record_for(self, cid: str) -> ChangeRecord | None:
        return self.records.get(cid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "added": sorted(self.added),
            "deleted": sorted(self.deleted),
            "interface": sorted(self.interface),
            "body": sorted(self.body),
            "edge": sorted(self.edge),
            "renamed": dict(sorted(self.renamed.items())),
        }


def _pair_renames(
    deleted: set[str],
    added: set[str],
    old: dict[str, Node],
    new: dict[str, Node],
    tau_ren: float,
) -> list[tuple[str, str, float]]:
    """Greedy best-match pairing of deleted x added by body similarity."""
    if not deleted or not added:
        return []
    candidates: list[tuple[float, str, str]] = []
    added_by_type: dict[str, list[str]] = {}
    for cid in added:
        added_by_type.setdefault(new[cid].component_type, []).append(cid)
    for old_id in deleted:
        o = old[old_id]
        o_body = normalize_body(o.source_code)
        if not o_body:
            continue
        for new_id in added_by_type.get(o.component_type, []):
            n_body = normalize_body(new[new_id].source_code)
            if not n_body:
                continue
            ratio = (
                len(o_body) / len(n_body)
                if len(n_body) >= len(o_body)
                else len(n_body) / len(o_body)
            )
            if ratio < tau_ren * 0.9:
                continue  # lengths too different to reach tau_ren
            sim = body_similarity(o.source_code, new[new_id].source_code)
            if sim >= tau_ren:
                candidates.append((sim, old_id, new_id))
    candidates.sort(reverse=True)
    used_old: set[str] = set()
    used_new: set[str] = set()
    pairs: list[tuple[str, str, float]] = []
    for sim, old_id, new_id in candidates:
        if old_id in used_old or new_id in used_new:
            continue
        used_old.add(old_id)
        used_new.add(new_id)
        pairs.append((old_id, new_id, sim))
    return pairs


def diff_graphs(
    old: dict[str, Node], new: dict[str, Node], opts: UpdateOptions | None = None
) -> GraphDiff:
    """Compute the component-level change set between two graphs."""
    opts = opts or UpdateOptions()
    result = GraphDiff()
    old_ids, new_ids = set(old), set(new)

    result.added = new_ids - old_ids
    result.deleted = old_ids - new_ids

    for cid in old_ids & new_ids:
        o, n = old[cid], new[cid]
        sig_changed = signature(o) != signature(n)
        hash_changed = body_hash(o) != body_hash(n)
        edges_changed = set(o.depends_on or ()) != set(n.depends_on or ())
        if not (sig_changed or hash_changed or edges_changed):
            continue
        if sig_changed:
            klass = CLASS_IFACE
            result.interface.add(cid)
        elif hash_changed:
            klass = CLASS_BODY
            result.body.add(cid)
        else:
            klass = CLASS_EDGE
            result.edge.add(cid)
        rec = ChangeRecord(
            component_id=cid,
            change_class=klass,
            old_id=cid,
            new_id=cid,
            relative_path=n.relative_path,
            component_type=n.component_type,
            old_signature=signature(o),
            new_signature=signature(n),
            signature_changed=sig_changed,
            edges_added=sorted(set(n.depends_on or ()) - set(o.depends_on or ())),
            edges_removed=sorted(set(o.depends_on or ()) - set(n.depends_on or ())),
        )
        if hash_changed:
            rec.diff = truncate_to_tokens(unified_body_diff(o, n, cid), opts.max_diff_tokens)
        result.records[cid] = rec

    for old_id, new_id, sim in _pair_renames(result.deleted, result.added, old, new, opts.tau_ren):
        result.deleted.discard(old_id)
        result.added.discard(new_id)
        result.renamed[old_id] = new_id
        o, n = old[old_id], new[new_id]
        sig_changed = signature(o) != signature(n)
        rec = ChangeRecord(
            component_id=new_id,
            change_class=CLASS_RENAMED,
            old_id=old_id,
            new_id=new_id,
            relative_path=n.relative_path,
            component_type=n.component_type,
            old_signature=signature(o),
            new_signature=signature(n),
            similarity=round(sim, 4),
            signature_changed=sig_changed,
        )
        if body_hash(o) != body_hash(n):
            rec.diff = truncate_to_tokens(unified_body_diff(o, n, new_id), opts.max_diff_tokens)
        result.records[new_id] = rec
        if sig_changed:
            # A renamed component whose contract also moved counts as an
            # interface change for its dependents.
            result.interface.add(new_id)

    for cid in result.added:
        n = new[cid]
        result.records[cid] = ChangeRecord(
            component_id=cid,
            change_class=CLASS_ADDED,
            new_id=cid,
            relative_path=n.relative_path,
            component_type=n.component_type,
            new_signature=signature(n),
            diff=truncate_to_tokens(unified_body_diff(None, n, cid), opts.max_diff_tokens),
        )
    for cid in result.deleted:
        o = old[cid]
        result.records[cid] = ChangeRecord(
            component_id=cid,
            change_class=CLASS_DELETED,
            old_id=cid,
            relative_path=o.relative_path,
            component_type=o.component_type,
            old_signature=signature(o),
            diff=truncate_to_tokens(unified_body_diff(o, None, cid), opts.max_diff_tokens),
        )

    logger.info("Graph diff: %s", result.counts())
    return result
