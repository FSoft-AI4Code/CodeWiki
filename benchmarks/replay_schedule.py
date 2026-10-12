"""Replay a real `codewiki generate` log through the concurrent scheduler.

Rebuilds, from a serial (concurrency 1) run's log, every agent the run started
(top-level leaf agents, nested sub-agents, parent overviews) and how long each
one worked on its own, then replays that tree through the real
DocumentationGenerator scheduler and AgentSlots with a fake backend that
sleeps instead of calling a model. Time is scaled (default: 1 s of the log =
1 ms here), so a 7-hour run replays in seconds.

It answers "how much faster would this exact run have been at concurrency N",
assuming each agent takes as long as it did serially (no provider rate limits).

    python benchmarks/replay_schedule.py codewiki-logs.txt --concurrency 1,2,4,8,16
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.documentation_generator import DocumentationGenerator

LINE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]\s+\w+\s+(.*)$")
LEAF = re.compile(r"📄 Processing leaf module: (.+)$")
SUB = re.compile(r"^( *)└─ Generating documentation for sub-module: (.+)$")
PARENT = re.compile(r"📁 Processing parent module: (.+)$")
END = re.compile(r"📚 Generating repository overview")


@dataclass
class Agent:
    name: str
    start: float
    own: float = 0.0  # seconds spent working itself, children excluded
    children: list["Agent"] = field(default_factory=list)


def parse_log(text: str):
    """Return (top-level module tree, {path: Agent}, {parent path: seconds})."""
    events = []
    day = 0
    last = -1.0
    for raw in text.splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + day
        if t < last:  # past midnight
            day += 86400
            t += 86400
        last = t
        msg = m[4]
        if mm := LEAF.search(msg):
            events.append(("leaf", t, mm[1].strip(), 0))
        elif mm := SUB.match(msg):
            events.append(("sub", t, mm[2].strip(), len(mm[1]) // 2))
        elif mm := PARENT.search(msg):
            events.append(("parent", t, mm[1].strip(), 0))
        elif END.search(msg):
            events.append(("end", t, "", 0))

    agents: dict[tuple[str, ...], Agent] = {}
    overviews: dict[tuple[str, ...], float] = {}
    tree: dict = {}
    stack: list[Agent] = []  # current agent chain, by depth
    for (kind, t, name, depth), nxt in zip(events, events[1:] + [("end", events[-1][1], "", 0)]):
        span = max(0.0, nxt[1] - t)
        if kind == "leaf":
            path = tuple(name.split("/"))
            node = tree
            for part in path[:-1]:
                node = node.setdefault(part, {"components": [], "children": {}})["children"]
            node[path[-1]] = {"components": [], "children": {}}
            agent = Agent(name=path[-1], start=t, own=span)
            agents[path] = agent
            stack = [agent]
        elif kind == "sub":
            agent = Agent(name=name, start=t, own=span)
            parent = stack[min(depth, len(stack)) - 1]
            parent.children.append(agent)
            stack = stack[:depth] + [agent]
        elif kind == "parent":
            overviews[tuple(name.split("/"))] = span
            stack = []
    return tree, agents, overviews


class ReplayBackend(LLMBackend):
    """Sleeps for each agent's own time; mirrors CawBackend's slot handling."""

    def __init__(self, config, agents, overviews, scale):
        self._config = config
        self.agents = agents
        self.overviews = overviews
        self.scale = scale
        self.peak = 0
        self._active = 0
        self._lock = __import__("threading").Lock()

    def _work(self, seconds: float) -> None:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        time.sleep(seconds * self.scale)
        with self._lock:
            self._active -= 1

    def _agent_sync(self, agent: Agent, hold: bool = True) -> None:
        with self.slots.hold() if hold else contextlib.nullcontext():
            self._work(agent.own)
            if agent.children:
                with self.slots.lend():
                    workers = min(len(agent.children), self.slots.size)
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        list(pool.map(self._agent_sync, agent.children))

    async def run_module_agent(
        self, module_name, components, core_component_ids, module_path, working_dir
    ):
        agent = self.agents[tuple(module_path)]
        async with self.slots.hold_async():
            await asyncio.to_thread(self._agent_sync, agent, False)
        Path(working_dir, f"{module_name}.md").write_text(f"# {module_name}\n")
        return {}

    def complete(self, prompt, *, model=None, system_prompt=None):
        name = re.search(r'"([^"]+)":\s*\{[^{}]*"is_target_for_overview_generation"', prompt)
        seconds = next(
            (s for path, s in self.overviews.items() if name and path[-1] == name[1]),
            0.0,
        )
        self._work(seconds)
        return "<OVERVIEW>replayed</OVERVIEW>"


def replay(tree, agents, overviews, concurrency: int, scale: float) -> tuple[float, int]:
    with tempfile.TemporaryDirectory() as docs:
        Path(docs, "module_tree.json").write_text(json.dumps(tree))
        config = SimpleNamespace(
            docs_dir=docs,
            repo_path=docs,
            get_prompt_addition=lambda: "",
            layout="flat",
            concurrency=concurrency,
        )
        gen = object.__new__(DocumentationGenerator)
        gen.config = config
        gen.backend = ReplayBackend(config, agents, overviews, scale)
        started = time.perf_counter()
        asyncio.run(gen.generate_module_documentation({}, []))
        return (time.perf_counter() - started) / scale, gen.backend.peak


def _count(agents) -> int:
    def walk(a):
        return 1 + sum(walk(c) for c in a.children)

    return sum(walk(a) for a in agents.values())


def _critical_path(agents, overviews) -> float:
    def walk(a):
        return a.own + max((walk(c) for c in a.children), default=0.0)

    return max(walk(a) for a in agents.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--concurrency", default="1,2,4,8,16")
    parser.add_argument("--scale", type=float, default=0.001, help="replay seconds per log second")
    ns = parser.parse_args()

    tree, agents, overviews = parse_log(ns.log.read_text(errors="replace"))

    def total(a):
        return a.own + sum(total(c) for c in a.children)

    serial = sum(total(a) for a in agents.values()) + sum(overviews.values())
    print(
        f"{len(agents)} top-level leaf agents, {_count(agents)} agent runs, "
        f"{len(overviews)} parent overviews; serial work {serial / 60:.1f} min; "
        f"longest single agent chain {_critical_path(agents, overviews) / 60:.1f} min"
    )
    print(f"\n{'concurrency':>11} | {'replayed':>9} | {'speedup':>7} | peak sessions")
    base = None
    for c in [int(x) for x in ns.concurrency.split(",")]:
        seconds, peak = replay(tree, agents, overviews, c, ns.scale)
        base = base or seconds
        print(f"{c:>11} | {seconds / 60:>7.1f}m | {base / seconds:>6.2f}x | {peak}")


if __name__ == "__main__":
    main()
