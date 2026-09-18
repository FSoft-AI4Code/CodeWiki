"""End-to-end run of the incremental updater on the toy repository with a fake backend."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

from updater_toy import OAUTH, USER, graph_r1, graph_r2, tracked_r2, tree_r1, write_pages

from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor
from codewiki.src.be.backend import AgentReply
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.updater.graph_store import save_graph
from codewiki.src.be.updater.options import UpdateOptions
from codewiki.src.be.updater.orchestrator import IncrementalUpdater
from codewiki.src.be.updater.record import RECORD_FILENAME


class FakeBackend:
    """Writes pages the way the real agents do (through the editor tool)."""

    def __init__(self, own_verdict="patch", route_to=None):
        self.own_verdict = own_verdict
        self.route_to = route_to  # leaf name the routing agent answers, None = untracked
        self.update_calls = []
        self.module_calls = []
        self.complete_calls = []
        self.routing_prompts = []
        self.last_usage = None

    def complete(self, prompt, *, model=None):
        self.complete_calls.append(prompt[:80])
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if "<CHANGED_UNTRACKED>" in prompt or "<ORPHANS>" in prompt:
            self.routing_prompts.append(prompt)
            ids = re.findall(r'<ORPHAN id="([^"]+)"', prompt)
            if self.route_to:
                decisions = [
                    {"component_id": c, "action": "place", "leaf": self.route_to, "reason": "fake"}
                    for c in ids
                ]
            else:
                decisions = [{"component_id": c, "action": "untracked"} for c in ids]
            return "```json\n" + json.dumps({"decisions": decisions}) + "\n```"
        return "<OVERVIEW>regenerated overview</OVERVIEW>"

    async def run_module_agent(
        self, module_name, components, core_component_ids, module_path, working_dir
    ):
        self.module_calls.append(module_name)
        page = Path(working_dir) / f"{module_name}.md"
        if not page.exists():
            page.write_text(f"# {module_name}\n\nregenerated with {sorted(core_component_ids)}\n")
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
        return json.load(open(os.path.join(working_dir, "module_tree.json")))

    async def run_update_agent(self, system_prompt, user_prompt, deps):
        self.update_calls.append((deps.current_module_name, sorted(deps.allowed_write_paths)))
        ctx = SimpleNamespace(deps=deps)
        verdicts = {}
        own = deps.current_module_name
        # Try to touch a page outside the write set: the tool must refuse.
        outsider = "storage" if own != "storage" else "core"
        out = await str_replace_editor(
            ctx, "docs", "insert", path=f"{outsider}.md", insert_line=1, new_str="SNEAK\n"
        )
        assert "not in this agent's write set" in out or "does not exist" in out
        for path in sorted(deps.allowed_write_paths):
            stem = Path(path).stem
            if stem == own and self.own_verdict == "rewrite":
                verdicts[f"{stem}.md"] = {"verdict": "rewrite", "reason": "too much changed"}
                continue
            if os.path.exists(path):
                await str_replace_editor(
                    ctx,
                    "docs",
                    "insert",
                    path=f"{stem}.md",
                    insert_line=1,
                    new_str=f"<!-- patched for {own} -->\n",
                )
                verdicts[f"{stem}.md"] = {"verdict": "patch", "reason": f"updated for {own}"}
            else:
                verdicts[f"{stem}.md"] = {"verdict": "no-op", "reason": "missing"}
        text = "done\n```json\n" + json.dumps({"verdicts": verdicts, "notes": ""}) + "\n```"
        return AgentReply(
            text=text, usage={"prompt_tokens": 30, "completion_tokens": 10}, seconds=0.01
        )


def _setup(tmp_path, tree=None, with_graph=True):
    docs = tmp_path / "docs"
    repo = tmp_path / "repo"
    repo.mkdir()
    write_pages(docs)
    (docs / "module_tree.json").write_text(json.dumps(tree if tree is not None else tree_r1()))
    (docs / "metadata.json").write_text(json.dumps({"generation_info": {"commit_id": "old"}}))
    graph_dir = docs / "temp" / "dependency_graphs"
    graph_dir.mkdir(parents=True)
    prev = graph_dir / "repo_dependency_graph.prev.json"
    if with_graph:
        save_graph(graph_r1(), str(prev))
    config = SimpleNamespace(
        docs_dir=str(docs),
        repo_path=str(repo),
        dependency_graph_dir=str(graph_dir),
        max_depth=5,
        cluster_model=None,
        main_model="fake",
        max_token_per_module=36369,
        get_prompt_addition=lambda: None,
    )
    return docs, config, str(prev)


def _generator(config, backend):
    gen = object.__new__(DocumentationGenerator)
    gen.config = config
    gen.backend = backend
    gen.commit_id = "new"
    return gen


def _run(docs, config, prev, backend, opts):
    gen = _generator(config, backend)
    upd = IncrementalUpdater(config, backend, gen, opts)
    return asyncio.run(
        upd.run(prev, graph_r2(), sorted(tracked_r2()), {"old_commit": "old", "new_commit": "new"})
    )


def test_toy_incremental_run_with_rewrite(tmp_path):
    docs, config, prev = _setup(tmp_path)
    backend = FakeBackend(own_verdict="rewrite")
    rec = _run(docs, config, prev, backend, UpdateOptions(tau_full=2.0, tau_tree=2.0))

    assert rec.outcome == "incremental"
    assert rec.diff["counts"] == {
        "added": 1,
        "deleted": 1,
        "interface": 1,
        "body": 1,
        "edge": 0,
        "renamed": 0,
    }
    order = [a["leaf"] for a in rec.active]
    assert order.index("core/auth") < order.index("core/api")
    assert order[-1] == "storage" and rec.active[-1]["mode"] == "delete"
    # deleted leaf page gone, its tree entry gone, the new class routed into auth
    assert not (docs / "storage.md").exists()
    tree = json.load(open(docs / "module_tree.json"))
    assert "storage" not in tree
    assert (
        OAUTH in tree["core"]["children"]["auth"]["components"]
        and USER not in tree["core"]["components"]
    )
    # auth was rewritten by the normal module agent after the agent's verdict
    assert "auth" in backend.module_calls
    assert "regenerated with" in (docs / "auth.md").read_text()
    verdicts = {(v["page"], v["by_leaf"]): v["verdict"] for v in rec.verdicts}
    assert verdicts[("auth", "auth")] == "rewrite"
    assert verdicts[("api", "auth")] == "patch"  # dependent, patched by auth's agent
    assert verdicts[("pipeline", "auth")] == "patch"  # referrer
    assert verdicts[("core", "auth")] == "patch" and verdicts[("overview", "auth")] == "patch"
    assert verdicts[("overview", "storage")] == "patch"
    # write sets recorded and respected
    assert set(rec.write_sets["auth"]) == {"auth", "core", "overview", "api", "pipeline"}
    assert set(rec.write_sets["storage"]) == {"overview"}
    assert rec.write_set_violations == []
    written = set(rec.pages_written)
    allowed = set().union(*[set(v) for v in rec.write_sets.values()]) | {"auth"}
    assert written <= allowed
    # every page in the tree exists; record, index and metadata summary present
    assert (docs / RECORD_FILENAME).exists()
    assert (docs / "temp" / "reference_index.json").exists()
    assert all((docs / f"{s}.md").exists() for s in ("auth", "api", "core", "pipeline", "overview"))
    kinds = [c["kind"] for c in rec.calls]
    assert "leaf_agent" in kinds and "rewrite" in kinds and "missing_pages" in kinds
    assert all(c["usage"] for c in rec.calls if c["kind"] in ("leaf_agent", "rewrite"))
    assert rec.stale_scan["scanned"] >= 0


def test_no_change_short_circuits(tmp_path):
    docs, config, prev = _setup(tmp_path)
    save_graph(graph_r1(), prev)
    backend = FakeBackend()
    gen = _generator(config, backend)
    upd = IncrementalUpdater(config, backend, gen, UpdateOptions())
    rec = asyncio.run(upd.run(prev, graph_r1(), sorted(graph_r1()), {}))
    assert rec.outcome == "no_change"
    assert backend.update_calls == [] and backend.module_calls == []
    assert (docs / "storage.md").exists()


def test_missing_old_graph_is_a_detector_failure(tmp_path):
    docs, config, prev = _setup(tmp_path, with_graph=False)
    backend = FakeBackend()
    rec = _run(docs, config, prev, backend, UpdateOptions())
    assert rec.outcome == "detector_failure"
    assert rec.errors and backend.update_calls == []
    assert (docs / "storage.md").exists()  # nothing touched


def test_fallback_fires_with_default_thresholds(tmp_path):
    docs, config, prev = _setup(tmp_path)
    backend = FakeBackend()
    rec = _run(docs, config, prev, backend, UpdateOptions())
    assert rec.outcome == "full_fallback"
    assert rec.fallback["fired"] and rec.fallback["r_leaf"] >= 0.5
    assert backend.update_calls == [] and backend.module_calls == []
    # tree on disk untouched, pages untouched
    assert "storage" in json.load(open(docs / "module_tree.json"))
    assert (docs / "storage.md").exists()


def test_rung_1_regenerates_instead_of_patching(tmp_path):
    docs, config, prev = _setup(tmp_path)
    backend = FakeBackend()
    opts = UpdateOptions.from_rung(1, tau_full=2.0, tau_tree=2.0)
    rec = _run(docs, config, prev, backend, opts)
    assert rec.outcome == "incremental"
    assert backend.update_calls == []  # no editing agent at rung 1
    assert "auth" in backend.module_calls  # rewritten
    # ancestors were invalidated and regenerated through complete()
    assert "regenerated overview" in (docs / "core.md").read_text()
    assert "regenerated overview" in (docs / "overview.md").read_text()
    # api's Up is empty at rung 1: page untouched
    assert (docs / "api.md").read_text().startswith("# api")


def test_whole_repo_mode(tmp_path):
    docs, config, prev = _setup(tmp_path, tree={})
    for stem in ("auth", "api", "core", "storage", "pipeline"):
        (docs / f"{stem}.md").unlink()
    backend = FakeBackend()
    rec = _run(docs, config, prev, backend, UpdateOptions())
    assert rec.outcome == "incremental"
    assert [a["page"] for a in rec.active] == ["overview"]
    assert json.load(open(docs / "module_tree.json")) == {}
    assert backend.update_calls and backend.update_calls[0][0] == "overview"


# ------------------------------------------------------------ ownership closure
HELPER = "src/misc/util.py::helper"  # fresh dir, no tracked neighbour: rules 1-3 fail


def _graphs_with_untracked_helper():
    from updater_toy import node

    old_g = graph_r1()
    old_g[HELPER] = node(HELPER, "function", "def helper():\n    return 1\n")
    new_g = {k: v.model_copy(deep=True) for k, v in old_g.items()}
    new_g[HELPER] = node(HELPER, "function", "def helper():\n    return 2\n")
    return old_g, new_g


def _run_untracked(tmp_path, backend, opts):
    docs, config, prev = _setup(tmp_path)
    old_g, new_g = _graphs_with_untracked_helper()
    save_graph(old_g, prev)
    gen = _generator(config, backend)
    upd = IncrementalUpdater(config, backend, gen, opts)
    tracked = sorted(set(old_g) - {HELPER})
    rec = asyncio.run(upd.run(prev, new_g, tracked, {"old_commit": "old", "new_commit": "new"}))
    return docs, rec


def test_ownership_closure_rule_4_activates_leaf(tmp_path):
    backend = FakeBackend(route_to="api")
    docs, rec = _run_untracked(tmp_path, backend, UpdateOptions())
    assert rec.outcome == "incremental"
    assert rec.diff["counts"]["body"] == 1
    # one routing call, in ownership mode (no "create" offered)
    assert len(backend.routing_prompts) == 1
    assert "<CHANGED_UNTRACKED>" in backend.routing_prompts[0]
    assert "create" not in backend.routing_prompts[0].split("For each component")[1]
    assert rec.ownership == [
        {
            "component_id": HELPER,
            "rule": "4:routing_agent",
            "leaf_path": ["core", "api"],
            "detail": "fake",
            "deleted": False,
        }
    ]
    routing_calls = [c for c in rec.calls if c["kind"] == "routing"]
    assert len(routing_calls) == 1 and routing_calls[0]["target"] == "1 changed untracked"
    # the leaf is active, its report shows the adopted id, the agent ran and patched
    assert [a["leaf"] for a in rec.active] == ["core/api"]
    assert rec.reports["core/api"]["own"] == [HELPER]
    assert rec.reports["core/api"]["adopted"] == {HELPER: "4:routing_agent"}
    assert backend.update_calls and backend.update_calls[0][0] == "api"
    assert "<!-- patched for api -->" in (docs / "api.md").read_text()
    # decision 2: the tree on disk is unchanged
    assert json.load(open(docs / "module_tree.json")) == tree_r1()
    assert rec.summary()["n_adopted"] == 1 and rec.summary()["n_adopted_by_agent"] == 1


def test_ownership_closure_agent_leaves_untracked(tmp_path):
    backend = FakeBackend(route_to=None)
    docs, rec = _run_untracked(tmp_path, backend, UpdateOptions())
    assert rec.outcome == "incremental"
    assert len(backend.routing_prompts) == 1
    assert rec.ownership == [] and rec.active == [] and backend.update_calls == []
    assert (docs / "api.md").read_text().startswith("# api\n\nRoutes call")


def test_ownership_closure_switched_off(tmp_path):
    backend = FakeBackend(route_to="api")
    docs, rec = _run_untracked(tmp_path, backend, UpdateOptions(use_ownership_closure=False))
    assert backend.routing_prompts == [] and rec.ownership == [] and rec.active == []
    (tmp_path / "r1").mkdir()
    backend = FakeBackend(route_to="api")
    docs, rec = _run_untracked(tmp_path / "r1", backend, UpdateOptions.from_rung(1))
    assert backend.routing_prompts == [] and rec.ownership == [] and rec.active == []
