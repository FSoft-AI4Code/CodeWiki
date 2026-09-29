"""Hyperparameters and ablation rungs for the incremental updater.

Every threshold the method depends on lives here with its default. The
ablation ladder (rungs 0..3 and 3b) is expressed as a preset over the same
fields so a run is fully described by one ``UpdateOptions`` value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

VALID_RUNGS = ("0", "1", "2", "3", "3b")


@dataclass
class UpdateOptions:
    # Ablation rung. "0" = legacy file-level invalidation (not handled by this
    # package), "3" = the proposed method, "3b" = rung 3 with k_hop = 2.
    rung: str = "3"

    # Step 1: graph diff
    tau_ren: float = 0.95  # body similarity to call a delete+add pair a rename
    max_diff_tokens: int = 8000  # cap on one component diff inside a report

    # Step 2: tree repair
    tau_nb: float = 0.5  # share of graph neighbours in one leaf to route there
    tau_grow: float = 0.33  # share of new components that triggers re-clustering

    # Step 3: change report
    k_hop: int = 1  # dependency hops followed for Up
    # Step 3a: ownership closure. A changed component that no leaf tracks gets
    # an effective owner by the placement rules of Step 2 (same file, same
    # directory, neighbour majority, routing agent) and enters that leaf's Own.
    # Activation only: the tree is not changed.
    use_ownership_closure: bool = True

    # Step 4: fallback
    tau_full: float = 0.5  # active leaves / all leaves
    tau_tree: float = 0.3  # (created + deleted + re-clustered) / all leaves

    # Derived toggles, set by ``from_rung`` (kept as fields so a record shows them).
    use_routing_agent: bool = True  # rule 4 orphans go to an agent
    use_growth_recluster: bool = True
    use_up: bool = True  # dependents receive Up
    agent_may_patch_leaf: bool = True  # False = rewrite the leaf page always
    agent_patches_related: bool = True  # False = delete ancestors and regenerate
    use_stale_scan: bool = True

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_rung(cls, rung: str | int, **overrides: Any) -> "UpdateOptions":
        rung = str(rung)
        if rung not in VALID_RUNGS:
            raise ValueError(f"unknown update rung {rung!r}; expected one of {VALID_RUNGS}")
        opts = cls(rung=rung)
        if rung == "1":
            opts.use_routing_agent = False
            opts.use_growth_recluster = False
            opts.use_up = False
            opts.agent_may_patch_leaf = False
            opts.agent_patches_related = False
            opts.use_stale_scan = False
            opts.use_ownership_closure = False
        elif rung == "2":
            opts.agent_may_patch_leaf = False
        elif rung == "3b":
            opts.k_hop = 2
        for key, value in overrides.items():
            if value is None:
                continue
            if not hasattr(opts, key):
                raise ValueError(f"unknown update option {key!r}")
            setattr(opts, key, value)
        return opts

    @property
    def is_legacy(self) -> bool:
        return self.rung == "0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
