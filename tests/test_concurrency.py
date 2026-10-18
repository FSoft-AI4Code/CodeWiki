"""Concurrent module generation: slots, the shared module tree, the scheduler."""

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from codewiki.src.be.agent_slots import AgentSlots, SharedModuleTree, merge_module_tree
from codewiki.src.be.agent_tools.str_replace_editor import EditTool
from codewiki.src.be.documentation_generator import DocumentationGenerator


def test_str_replace_that_empties_a_file_does_not_crash(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("only line")
    tool = EditTool({}, str(tmp_path))
    tool(command="str_replace", path=str(page), old_str="only line", new_str="")
    assert page.read_text() == ""
    assert "has been edited" in "\n".join(tool.logs)


def test_slots_cap_concurrent_holders():
    slots = AgentSlots(2)
    active, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal active, peak
        with slots.hold():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


def test_lent_slot_lets_children_run_with_one_slot():
    # A parent holding the only slot must not deadlock waiting on its child.
    slots = AgentSlots(1)
    done = []

    def child():
        with slots.hold():
            done.append("child")

    def parent():
        with slots.hold():
            with slots.lend():
                t = threading.Thread(target=child)
                t.start()
                t.join(timeout=2)
            done.append("parent")

    t = threading.Thread(target=parent)
    t.start()
    t.join(timeout=3)
    assert done == ["child", "parent"]


def test_merge_module_tree_keeps_both_sides_sub_modules():
    disk = {"a": {"components": [], "children": {"a1": {"components": [], "children": {}}}}}
    mine = {
        "a": {"components": [], "children": {"a2": {"components": [], "children": {}}}},
        "b": {"components": [], "children": {}},
    }
    merge_module_tree(disk, mine)
    assert set(disk) == {"a", "b"}
    assert set(disk["a"]["children"]) == {"a1", "a2"}


def test_shared_tree_save_does_not_drop_other_agents_modules(tmp_path):
    path = str(tmp_path / "module_tree.json")
    Path(path).write_text(json.dumps({"a": {"components": [], "children": {}}}))
    shared = SharedModuleTree()
    # Another writer's private copy, saved after this tree was loaded.
    stale = {"b": {"components": [], "children": {}}}
    live = shared.get(path)
    live["a"]["children"]["a1"] = {"components": [], "children": {}}
    shared.save(path, stale)
    saved = json.loads(Path(path).read_text())
    assert set(saved) == {"a", "b"}
    assert "a1" in saved["a"]["children"]


class _SlowBackend:
    """Writes a page per agent call after a delay, recording overlap."""

    def __init__(self, delay: float):
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.finished: list[str] = []

    async def run_module_agent(
        self, module_name, components, core_component_ids, module_path, working_dir
    ):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        Path(working_dir, f"{module_name}.md").write_text(f"# {module_name}\n")
        self.finished.append(module_name)
        return json.loads(Path(working_dir, "module_tree.json").read_text())

    def complete(self, prompt, model=None, system_prompt=None):
        self.finished.append("<overview>")
        return "<OVERVIEW>overview</OVERVIEW>"


def _run(tmp_path, concurrency):
    leaf = {"components": [], "children": {}}
    tree = {
        "parent": {"components": [], "children": {f"c{i}": dict(leaf) for i in range(4)}},
        **{f"m{i}": dict(leaf) for i in range(4)},
    }
    (tmp_path / "module_tree.json").write_text(json.dumps(tree))
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        docs_dir=str(tmp_path),
        repo_path=str(tmp_path),
        get_prompt_addition=lambda: "",
        layout="flat",
        concurrency=concurrency,
    )
    gen.backend = _SlowBackend(delay=0.05)
    started = time.perf_counter()
    asyncio.run(gen.generate_module_documentation({}, []))
    return gen.backend, time.perf_counter() - started


def test_scheduler_runs_independent_modules_concurrently(tmp_path):
    backend, elapsed = _run(tmp_path, concurrency=4)
    assert backend.peak == 4
    assert elapsed < 8 * 0.05  # 8 leaves serially would take 0.4 s
    assert (tmp_path / "parent.md").exists()
    assert (tmp_path / "overview.md").exists()


def test_scheduler_writes_parent_after_all_its_children(tmp_path):
    backend, _ = _run(tmp_path, concurrency=3)
    # The parent page comes from complete(), which runs after its children.
    first_overview = backend.finished.index("<overview>")
    assert {"c0", "c1", "c2", "c3"} <= set(backend.finished[:first_overview])


def test_scheduler_with_concurrency_one_is_serial(tmp_path):
    backend, _ = _run(tmp_path, concurrency=1)
    assert backend.peak == 1


class _FakeCawBackend:
    def __init__(self, size):
        self.slots = AgentSlots(size)
        self.module_tree = SharedModuleTree()
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _run_module_agent_sync(self, module_name, module_path, working_dir, **kwargs):
        with self.slots.hold():
            with self._lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.05)
            with self._lock:
                self.active -= 1
            Path(working_dir, f"{module_name}.md").write_text(f"# {module_name}\n")


def test_caw_sub_modules_run_side_by_side_within_the_slot_budget(tmp_path):
    from codewiki.src.be.agent_tools.deps import CodeWikiDeps
    from codewiki.src.be.caw_toolkit import CawToolKit

    backend = _FakeCawBackend(size=3)
    deps = CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components={},
        path_to_current_module=["parent"],
        current_module_name="parent",
        module_tree={"parent": {"components": [], "children": {}}},
        max_depth=2,
        current_depth=1,
        config=SimpleNamespace(layout="flat"),
    )
    toolkit = CawToolKit(deps=deps, backend=backend, allow_subagent=True)
    specs = {f"sub{i}": [f"f{i}.py::X"] for i in range(5)}

    with backend.slots.hold():  # the parent session's own slot
        report = toolkit._run_sub_modules(specs)

    assert backend.peak == 3
    assert set(deps.module_tree["parent"]["children"]) == set(specs)
    assert deps.path_to_current_module == ["parent"]
    assert all((tmp_path / f"sub{i}.md").exists() for i in range(5))
    assert "sub0" in report


# --- pydantic sub-module fan-out --------------------------------------------


def test_pydantic_sub_modules_run_concurrently_with_their_own_deps(tmp_path, monkeypatch):
    from codewiki.src.be.agent_tools import generate_sub_module_documentations as tool_mod
    from codewiki.src.be.agent_tools.deps import CodeWikiDeps
    from codewiki.src.be.generation_progress import GenerationProgress

    seen: list[tuple[str, list[str], int]] = []
    state = {"active": 0, "peak": 0}

    class FakeAgent:
        def __init__(self, *args, name=None, **kwargs):
            self.name = name

        async def run(self, prompt, deps, usage_limits=None):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            seen.append(
                (deps.current_module_name, list(deps.path_to_current_module), deps.current_depth)
            )
            await asyncio.sleep(0.05)
            Path(deps.absolute_docs_path, f"{deps.current_module_name}.md").write_text("# page\n")
            state["active"] -= 1

    monkeypatch.setattr(tool_mod, "Agent", FakeAgent)
    monkeypatch.setattr(tool_mod, "create_fallback_models", lambda config: None)

    messages: list[str] = []
    progress = GenerationProgress(concurrency=3, emit=messages.append)
    deps = CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components={},
        path_to_current_module=["parent"],
        current_module_name="parent",
        module_tree={"parent": {"components": [], "children": {}}},
        max_depth=2,
        current_depth=1,
        config=SimpleNamespace(
            layout="flat", max_token_per_leaf_module=4000, agent_retries=1, request_limit=5
        ),
        slots=AgentSlots(3),
        progress=progress,
    )
    specs = {f"sub{i}": [f"f{i}.py::X"] for i in range(5)}

    async def run():
        async with deps.slots.hold_async():  # the parent agent's own slot
            return await tool_mod.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), specs
            )

    report = asyncio.run(run())

    assert state["peak"] == 3
    assert sorted(name for name, _, _ in seen) == sorted(specs)
    assert all(path == ["parent", name] and depth == 2 for name, path, depth in seen)
    # The parent's own deps were not touched by its children.
    assert deps.path_to_current_module == ["parent"] and deps.current_module_name == "parent"
    assert set(deps.module_tree["parent"]["children"]) == set(specs)
    assert "sub0" in report
    assert progress.total == 5 and progress.done == 5
    assert len([m for m in messages if m.startswith("✓ parent/sub")]) == 5


# --- progress reporting -----------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_progress_status_line_and_eta():
    from codewiki.src.be.generation_progress import GenerationProgress

    clock, messages = _Clock(), []
    p = GenerationProgress(concurrency=2, emit=messages.append, clock=clock)
    p.add(5)
    assert "ETA after the first module" in p.status()

    with p.track("a"):
        clock.now += 120
    # 4 left x 120 s each / 2 at a time = 4 min
    assert messages[-1] == "✓ a (2m 00s) · 1/5 done (20%) · ETA ~4m 00s"

    p.add(3)  # an agent split a module into three sub-modules
    with p.track("b"):
        clock.now += 60
    # 2 finished (= concurrency): throughput, 2 in 180 s, 6 left
    assert messages[-1] == "✓ b (1m 00s) · 2/8 done (25%) · ETA ~9m 00s"


def test_progress_reports_failures_and_already_documented_pages():
    from codewiki.src.be.generation_progress import GenerationProgress

    clock, messages = _Clock(), []
    p = GenerationProgress(emit=messages.append, clock=clock)
    p.add(2)
    with p.track("cached"):
        clock.now += 0.1
    try:
        with p.track("broken"):
            clock.now += 30
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert messages[0].startswith("✓ cached (already documented) · 1/2 done (50%)")
    assert messages[1] == "✗ broken failed after 30s · 2/2 done (100%) · 1 failed"
    assert p.summary()[0].endswith("; 1 failed")


def test_progress_hears_caw_usage_limit_and_http_429():
    import logging

    from codewiki.src.be.generation_progress import GenerationProgress, notify_http_response

    messages = []
    p = GenerationProgress(concurrency=4, emit=messages.append)
    with p.activate():
        logging.getLogger("codewiki._vendor.caw.agent").warning(
            "Usage limit reached. Auto-waiting %s min before resuming.", 24
        )
        notify_http_response(429, "7")
        notify_http_response(429, "7")  # noted once a minute, still counted
        notify_http_response(200, None)
    notify_http_response(429, None)  # no active run: ignored
    assert messages[0].startswith("⏸ Usage limit reached: paused until ~")
    assert "(24 min)" in messages[0]
    assert messages[1].startswith(
        "⏳ Rate-limited by the provider (HTTP 429, 1 so far): retrying in 7s"
    )
    assert len(messages) == 2
    assert "2 rate-limited request(s)" in p.summary()[0]


def test_module_file_count_includes_sub_modules():
    from codewiki.cli.adapters.doc_generator import _module_file_count

    info = {
        "components": ["a.py::A", "a.py::B"],
        "children": {
            "x": {"components": ["b.py::C", "Dockerfile"], "children": {}},
            "y": {"components": ["a.py::D"], "children": {}},
        },
    }
    assert _module_file_count(info) == 3
    assert _module_file_count({"components": [], "children": {}}) == 0


def test_scheduler_prints_progress_and_summary(tmp_path):
    messages: list[str] = []
    leaf = {"components": [], "children": {}}
    tree = {"parent": {"components": [], "children": {"c0": dict(leaf), "c1": dict(leaf)}}}
    (tmp_path / "module_tree.json").write_text(json.dumps(tree))
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        docs_dir=str(tmp_path),
        repo_path=str(tmp_path),
        get_prompt_addition=lambda: "",
        layout="flat",
        concurrency=2,
    )
    gen.backend = _SlowBackend(delay=0.01)
    gen.progress_emit = messages.append
    asyncio.run(gen.generate_module_documentation({}, []))

    done_lines = [m for m in messages if m.startswith("✓ ")]
    # c0, c1, parent, repository overview
    assert len(done_lines) == 4
    assert done_lines[-1].startswith("✓ Repository overview")
    assert "4/4 done (100%)" in done_lines[-1]
    assert messages[-1].startswith("Documented 4 module page(s)") or messages[-2].startswith(
        "Documented 4 module page(s)"
    )


def test_running_count_comes_from_held_slots():
    from codewiki.src.be.generation_progress import GenerationProgress

    slots = AgentSlots(2)
    p = GenerationProgress(concurrency=2, emit=lambda m: None, active=lambda: slots.active)
    p.add(4)
    with slots.hold():
        assert "1 running" in p.status()
        with slots.lend():  # a parent waiting on its sub-modules
            assert "running" not in p.status()
    assert slots.active == 0


def test_pydantic_failed_sub_module_does_not_fail_its_parent(tmp_path, monkeypatch):
    from codewiki.src.be.agent_tools import generate_sub_module_documentations as tool_mod
    from codewiki.src.be.agent_tools.deps import CodeWikiDeps
    from codewiki.src.be.generation_progress import GenerationProgress

    class FakeAgent:
        def __init__(self, *args, name=None, **kwargs):
            pass

        async def run(self, prompt, deps, usage_limits=None):
            if deps.current_module_name == "bad":
                raise RuntimeError("Request timed out")
            Path(deps.absolute_docs_path, f"{deps.current_module_name}.md").write_text("# page\n")

    monkeypatch.setattr(tool_mod, "Agent", FakeAgent)
    monkeypatch.setattr(tool_mod, "create_fallback_models", lambda config: None)

    progress = GenerationProgress(concurrency=2, emit=lambda _m: None)
    deps = CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path=str(tmp_path),
        registry={},
        components={},
        path_to_current_module=["parent"],
        current_module_name="parent",
        module_tree={"parent": {"components": [], "children": {}}},
        max_depth=2,
        current_depth=1,
        config=SimpleNamespace(
            layout="flat", max_token_per_leaf_module=4000, agent_retries=1, request_limit=5
        ),
        slots=AgentSlots(2),
        progress=progress,
    )
    specs = {"good": ["a.py::X"], "bad": ["b.py::Y"]}

    async def run():
        async with deps.slots.hold_async():
            return await tool_mod.generate_sub_module_documentation(
                SimpleNamespace(deps=deps), specs
            )

    report = asyncio.run(run())  # no exception: the parent agent carries on

    assert (tmp_path / "good.md").exists() and not (tmp_path / "bad.md").exists()
    assert "bad" in report  # listed as missing for the parent agent
    # Still in the tree, so the next run regenerates it.
    assert "bad" in deps.module_tree["parent"]["children"]
    assert progress.done == 1 and progress.failed == 1
