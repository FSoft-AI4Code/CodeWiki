# Incremental updates

New in 2.0. `codewiki generate --update` refreshes existing documentation
after the code changed, and touches only the pages the change affects.

```bash
cd /path/to/your/project
codewiki generate            # first build
# ... code changes, commits ...
codewiki generate --update   # refresh
```

## What a build leaves behind

A full build writes these files into the docs folder. The updater reads all
of them.

| File | What it holds |
| --- | --- |
| `module_tree.json` | The module hierarchy. Each leaf module lists its component ids |
| `<module>.md` | One page per module, leaves and parents alike |
| `overview.md` | The root page |
| `temp/dependency_graphs/*.json` | The dependency graph: every component with its source, signature, and edges |
| `temp/artifact_index.json` | The artifact index |
| `metadata.json` | Build info, including the commit id |

## What `--update` does

The unit of change is the **component** (a class, a function, a build
file), not the source file. The unit of work is the **leaf module**.

1. **Diff the graphs.** Load the saved graph, build the graph for the current
   code, and join on component id. Each component is unchanged, body-changed,
   signature-changed, added, deleted, or renamed (a deleted and an added
   component with near-identical bodies). A file that changed in a comment
   only produces no changed components, and nothing downstream hears about it.
2. **Repair the module tree.** Renamed ids are rewritten. Deleted components
   leave their module. New components are routed to a module by rules (same
   file, same directory, majority of graph neighbours). What the rules cannot
   place goes to a small routing agent, which may create a new leaf module.
   A module that grew a lot gets its parent re-clustered.
3. **Build one change report per leaf module.** Its own changed components,
   upstream interface changes it depends on, changed things its page mentions,
   and components that entered or left.
4. **Decide whether incremental is worth it.** If too much changed, run a
   full build instead and record why (see thresholds below).
5. **One agent per active leaf, in dependency order.** The agent reads the
   report and the current page, then chooses: patch in place, rewrite, or
   no-op with a reason. It may also patch the pages of ancestors, dependents,
   and referrers, but only where they describe this module. It cannot write
   any other page.
6. **Finish.** Generate any page still missing (new parents), scan every
   untouched page for stale names and dead links, save the new graph, tree,
   and a record of every decision.

Pages outside the write sets are never touched. Prose about unchanged code
does not churn.

## When it falls back to a full build

Two ratios are checked before any agent runs:

| Ratio | Threshold | Flag |
| --- | --- | --- |
| active leaf modules / all leaf modules | 0.5 | `--tau-full` |
| (created + deleted + re-clustered modules) / all leaf modules | 0.3 | `--tau-tree` |

Above either threshold the updater runs a full build and writes
`full_fallback` with both ratios into the record. A saved graph that cannot
be loaded also leads to a full build, recorded as a detector failure.

## Comparing against a specific commit

By default the updater diffs against the commit stored in `metadata.json`.
In CI, or after a squashed merge, that commit may be gone. Pass the base
commit yourself:

```bash
codewiki generate --compare-to <commit-hash>   # implies --update
```

If Git cannot produce a diff at all, the updater still works: it compares
the saved dependency graph against the fresh one.

## Thresholds and knobs

All defaults come from the method's evaluation. Override them per run.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--tau-ren` | 0.95 | Body similarity above which a delete plus an add counts as a rename |
| `--tau-nb` | 0.5 | Share of graph neighbours in one module needed to route a new component there |
| `--tau-grow` | 0.33 | Share of new components in a module that triggers re-clustering of its parent |
| `--tau-full` | 0.5 | Active-module share that forces a full build |
| `--tau-tree` | 0.3 | Structural-change share that forces a full build |
| `--k-hop` | 1 | Dependency hops followed when collecting upstream interface changes |
| `--max-diff-tokens` | 8000 | Cap on one component diff inside a report |

## Updater variants (`--update-rung`)

| Rung | What runs |
| --- | --- |
| `0` | The 1.x updater: a changed file invalidates every module whose component ids contain that path, and those pages are rebuilt from scratch. Kept as a baseline |
| `1` | Graph diff and rule-based tree repair only. Hit pages are rewritten. No routing agent, no upstream propagation, no stale scan |
| `2` | Rung 1 plus routing agent, growth re-clustering, upstream propagation, related-page patching. The leaf page itself is always rewritten |
| `3` (default) | The full method. The agent may patch the leaf page in place |
| `3b` | Rung 3 following 2 dependency hops instead of 1 |

## The update record

Every step writes to `<output>/update_record.json` and a summary goes into
`metadata.json`. The record holds the outcome (`no_change`, `incremental`,
`full_fallback`, `detector_failure`), the change counts per class, every
routing decision, the active modules with their mode, the fallback ratios,
every per-page verdict with its reason, and the time and token cost of each
agent call. Every page written in an update can be explained after the fact.

## Cost

Measured on svelte (about 125k lines of JavaScript) with the same model:

| Run | Cost | Time |
| --- | --- | --- |
| Full build | $21.48 | 2 h 46 min |
| Update after one commit | $0.34 | 3 min |

Cost of an update scales with the change, not with the repository.
