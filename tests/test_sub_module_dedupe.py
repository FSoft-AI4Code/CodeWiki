"""The pydantic-ai sub-module tool never regenerates an already documented
module under a suffixed name (issue #113)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from codewiki.src.be.agent_tools import generate_sub_module_documentations as mod
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.dependency_analyzer.models.core import Node


class FakeAgent:
    """Stands in for pydantic_ai.Agent: writes the sub-module page and returns."""

    runs: list[str] = []

    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name")

    async def run(self, prompt, deps, **kwargs):
        FakeAgent.runs.append(deps.current_module_name)
        page = f"{deps.absolute_docs_path}/{deps.current_module_name}.md"
        with open(page, "w", encoding="utf-8") as f:
            f.write(f"# {deps.current_module_name}\n")
        return SimpleNamespace(output="ok")


def _node(cid: str) -> Node:
    rel, name = cid.split("::", 1)
    return Node(
        id=cid,
        name=name,
        component_type="function",
        file_path=f"/repo/{rel}",
        relative_path=rel,
        source_code=f"def {name}():\n    pass\n",
    )


def _deps(tmp_path) -> CodeWikiDeps:
    components = {c: _node(c) for c in ("a.py::fa", "b.py::fb")}
    return CodeWikiDeps(
        absolute_docs_path=str(tmp_path),
        absolute_repo_path="/repo",
        registry={},
        components=components,
        path_to_current_module=[],
        current_module_name="overview",
        module_tree={},
        max_depth=2,
        current_depth=1,
        config=SimpleNamespace(max_token_per_leaf_module=4000, agent_retries=3, request_limit=100),
        custom_instructions="",
    )


@pytest.fixture(autouse=True)
def _fake_agent(monkeypatch):
    FakeAgent.runs = []
    monkeypatch.setattr(mod, "Agent", FakeAgent)
    monkeypatch.setattr(mod, "create_fallback_models", lambda config: None)


def _call(deps, specs):
    return asyncio.run(mod.generate_sub_module_documentation(SimpleNamespace(deps=deps), specs))


def test_repeat_request_is_skipped_not_suffixed(tmp_path):
    deps = _deps(tmp_path)
    specs = {"approval": ["a.py::fa"], "billing": ["b.py::fb"]}

    first = _call(deps, specs)
    assert "approval.md" in first and "billing.md" in first
    assert (tmp_path / "approval.md").exists() and (tmp_path / "billing.md").exists()
    assert set(deps.module_tree) == {"approval", "billing"}

    # Round 2: same names again -> parent prefix, once (issue #76 behaviour).
    _call(deps, specs)
    assert (tmp_path / "overview_approval.md").exists()
    assert (tmp_path / "overview_billing.md").exists()

    # Round 3: plain and prefixed both exist -> skipped, nothing new anywhere.
    files_before = sorted(p.name for p in tmp_path.iterdir())
    tree_before = dict(deps.module_tree)
    runs_before = len(FakeAgent.runs)
    third = _call(deps, specs)
    assert "Skipped sub-modules" in third and "Do NOT call" in third
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    assert deps.module_tree == tree_before
    assert len(FakeAgent.runs) == runs_before
    assert not any(p.name.endswith("_2.md") for p in tmp_path.iterdir())


def test_mixed_batch_generates_only_new_names(tmp_path):
    deps = _deps(tmp_path)
    (tmp_path / "approval.md").write_text("old")
    (tmp_path / "overview_approval.md").write_text("old")
    report = _call(deps, {"approval": ["a.py::fa"], "billing": ["b.py::fb"]})
    assert (tmp_path / "billing.md").exists()
    assert FakeAgent.runs == ["billing"]
    assert "billing.md" in report and "'approval'" in report and "Skipped" in report
    assert set(deps.module_tree) == {"billing"}
