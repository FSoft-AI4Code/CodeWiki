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
