"""Component-level incremental updater (Contribution 3).

Given the graph, module tree, and pages left behind by a previous build,
and the fresh code graph of the new revision, this package:

1. diffs the two graphs component by component (``graph_diff``),
2. repairs the module tree (``tree_repair``),
3. builds a change report per leaf module (``change_report``),
4. decides whether an incremental update is worth doing (``change_report.fallback_ratios``),
5. runs one editing agent per active leaf, restricted to a write set (``leaf_agent``),
6. generates any page still missing, scans for stale names, and records
   every decision (``orchestrator``, ``stale_scan``, ``record``).

Rung 0 (the file-level cache invalidation that predates this package) stays
available through ``UpdateOptions(rung=0)`` and is implemented in
``codewiki/cli/commands/generate.py``.
"""

from codewiki.src.be.updater.options import UpdateOptions

__all__ = ["UpdateOptions"]
