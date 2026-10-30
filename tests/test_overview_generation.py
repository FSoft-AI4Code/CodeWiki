"""Regression coverage for missing context and incomplete pages in issue #134."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codewiki.src.be.backend import UsageLedger
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be import overview_support as support
from codewiki.src.be.prompt_template import MAX_USER_PROMPT_CHARS
from codewiki.src.be.utils import count_tokens

PREAMBLE = (
    "I'll start by reading the two child module documentation files.\n\n"
    "/opt/runner/documentation/build_and_deployment.md "
    "/opt/runner/documentation/bootstrap.md "
    "/opt/runner/documentation/build_and_deployment.md"
)
VALID = "# Visão geral\n\nO módulo coordena a implantação e inicialização.\n"


class Backend:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.usage_ledger = UsageLedger()

    def complete(self, prompt, *, system_prompt=None):
        self.calls.append((prompt, system_prompt))
        self.usage_ledger.add("complete", "", {"input_tokens": 10})
        return next(self.replies)


def generator(tmp_path, replies, layout="flat"):
    tree = {
        "parent": {
            "components": [],
            "children": {
                "build_and_deployment": {"components": [], "children": {}},
                "bootstrap": {"components": [], "children": {}},
            },
        }
    }
    (tmp_path / "module_tree.json").write_text(json.dumps(tree))
    gen = DocumentationGenerator.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        layout=layout, repo_path="/tmp/example", get_prompt_addition=lambda: "Write in Portuguese."
    )
    gen.backend = Backend(replies)
    return gen


@pytest.mark.parametrize("layout", ["flat", "hierarchical"])
def test_parent_receives_child_contents_and_relative_links(tmp_path, layout):
    gen = generator(tmp_path, [VALID], layout)
    child_dir = tmp_path if layout == "flat" else tmp_path / "parent"
    child_dir.mkdir(exist_ok=True)
    (child_dir / "build_and_deployment.md").write_text("# Build\n\nCHILD_CONTENT_SENTINEL")
    (child_dir / "bootstrap.md").write_text("# Bootstrap\n\nInitialize the plugin.")
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(tmp_path)))
    prompt, system = gen.backend.calls[0]
    assert "CHILD_CONTENT_SENTINEL" in prompt
    assert "Initialize the plugin" in prompt
    assert '"link": "' + ("parent/" if layout == "hierarchical" else "") + "bootstrap.md" in prompt
    assert "read those files with your" not in prompt
    assert "Portuguese" in system
    assert (tmp_path / "parent.md").read_text() == VALID.strip()


@pytest.mark.parametrize(
    "response",
    [
        PREAMBLE,
        "",
        None,
        "# Only a title",
        "```text\n# fake\nbody\n```",
        "# Title\n/absolute/file.md",
        "# Title\nrelative/file.md",
        "# Title\n" + PREAMBLE,
    ],
)
def test_invalid_output_is_corrected_once(tmp_path, response):
    gen = generator(tmp_path, [response, "<OVERVIEW>" + VALID + "</OVERVIEW>"])
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(tmp_path)))
    assert (tmp_path / "parent.md").read_text() == VALID.strip()
    assert len(gen.backend.calls) == 2
    assert "Validation failure" in gen.backend.calls[1][0]
    assert gen.backend.usage_ledger.summary()["n_calls"] == 2


@pytest.mark.parametrize("original", [None, "", PREAMBLE])
def test_exhausted_correction_never_overwrites_old_page(tmp_path, original):
    gen = generator(tmp_path, [PREAMBLE, PREAMBLE])
    path = tmp_path / "parent.md"
    if original is not None:
        path.write_text(original)
    with pytest.raises(RuntimeError, match="after one correction attempt"):
        asyncio.run(gen.generate_parent_module_docs(["parent"], str(tmp_path)))
    assert len(gen.backend.calls) == 2
    assert path.read_text() == original if original is not None else not path.exists()


@pytest.mark.parametrize("original", ["", PREAMBLE, PREAMBLE.replace("I'll", "I’ll")])
@pytest.mark.parametrize("module_path,filename", [([], "overview.md"), (["parent"], "parent.md")])
def test_recognized_old_pages_are_repaired(tmp_path, original, module_path, filename):
    gen = generator(tmp_path, [VALID])
    path = tmp_path / filename
    path.write_text(original)
    asyncio.run(gen.generate_parent_module_docs(module_path, str(tmp_path)))
    assert path.read_text() == VALID.strip()


@pytest.mark.parametrize(
    "original",
    [
        VALID,
        "A manually edited page without a heading.",
        PREAMBLE + "\n\nUseful documentation follows.",
    ],
)
def test_other_existing_prose_is_preserved(tmp_path, original):
    gen = generator(tmp_path, [])
    (tmp_path / "parent.md").write_text(original)
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(tmp_path)))
    assert not gen.backend.calls
    assert (tmp_path / "parent.md").read_text() == original


def test_missing_and_unreadable_children_warn_without_fabrication(tmp_path, monkeypatch, caplog):
    gen = generator(tmp_path, [VALID])
    path = tmp_path / "bootstrap.md"
    path.write_text("unreadable secret")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == "bootstrap.md":
            raise UnicodeError("invalid encoding")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(tmp_path)))
    assert "Cannot read child documentation" in caplog.text
    assert "Module docs not found" in caplog.text
    assert "unreadable secret" not in gen.backend.calls[0][0]
    assert "bootstrap.md" in gen.backend.calls[0][0]


def test_sanitized_child_filename_is_read(tmp_path):
    gen = generator(tmp_path, [VALID])
    path = tmp_path / "module_tree.json"
    tree = json.loads(path.read_text())
    tree["parent"]["children"]["Build Deployment"] = tree["parent"]["children"].pop(
        "build_and_deployment"
    )
    path.write_text(json.dumps(tree))
    (tmp_path / "build_deployment.md").write_text("# Build\n\nSANITIZED_CONTENT")
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(tmp_path)))
    assert "SANITIZED_CONTENT" in gen.backend.calls[0][0]


def test_context_budget_shares_space_and_redistributes_unused_allocations(monkeypatch):
    monkeypatch.setattr(support, "OVERVIEW_INPUT_TOKENS", 700)
    children = {
        "small": "Small complete page.",
        "large_a": "alpha " * 5000,
        "large_b": "beta " * 5000,
    }
    prompt = support.bounded_overview_prompt("Structure", children, "System instructions")
    assert count_tokens("System instructions" + prompt) <= 700
    assert "Small complete page." in prompt
    assert prompt.count("alpha") > 150 and prompt.count("beta") > 150
    assert prompt.count("Excerpt truncated") == 2


def test_large_unicode_context_and_correction_stay_within_caps():
    children = {"日本語": "日本語の説明。" * 20_000, "second": "source " * 50_000}
    prompt = support.bounded_overview_prompt(
        "Structure", children, "System", correction="invalid body", previous="draft " * 50_000
    )
    assert count_tokens("System" + prompt) <= support.OVERVIEW_INPUT_TOKENS
    assert len("System" + prompt) <= MAX_USER_PROMPT_CHARS
    assert "�" not in prompt
    assert "日本語の説明" in prompt and "source" in prompt


def test_mandatory_context_overflow_is_explicit(monkeypatch):
    monkeypatch.setattr(support, "OVERVIEW_INPUT_TOKENS", 100)
    with pytest.raises(RuntimeError, match="Mandatory overview context"):
        support.bounded_overview_prompt("structure " * 200, {}, "system")


def test_character_limit_also_bounds_whitespace_heavy_context(monkeypatch):
    monkeypatch.setattr(support, "MAX_USER_PROMPT_CHARS", 1000)
    prompt = support.bounded_overview_prompt("Structure", {"child": " " * 100_000}, "System")
    assert len("System" + prompt) <= 1000


def test_valid_prose_in_non_latin_script():
    assert support.overview_error("# 概要\n\nこれはシステムの説明です。") is None


def test_failed_atomic_replacement_preserves_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "overview.md"
    path.write_text(PREAMBLE)

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(support.os, "replace", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        support.save_overview(VALID, str(path))
    assert path.read_text() == PREAMBLE
    assert list(tmp_path.iterdir()) == [path]


def test_whole_repo_recovery_restores_old_page_on_failure(tmp_path):
    path = tmp_path / "overview.md"
    path.write_text(PREAMBLE)
    with pytest.raises(RuntimeError), support.preserve_incomplete_overviews([str(path)]):
        path.write_text("partial edit")
        raise RuntimeError("agent failed")
    assert path.read_text() == PREAMBLE


def test_parent_failure_does_not_skip_independent_modules_or_pass_silently(tmp_path):
    gen = generator(tmp_path, [])
    gen.config.docs_dir = str(tmp_path)
    tree_path = tmp_path / "module_tree.json"
    tree = json.loads(tree_path.read_text())
    tree["independent"] = {"components": [], "children": {}}
    tree_path.write_text(json.dumps(tree))
    (tmp_path / "parent.md").write_text(PREAMBLE)

    async def module(module_name, **kwargs):
        (tmp_path / f"{module_name}.md").write_text(VALID)
        return tree

    async def overview(module_path, working_dir, components=None):
        if module_path == ["parent"]:
            raise RuntimeError("invalid parent response")
        (tmp_path / "overview.md").write_text(VALID)
        return tree

    gen.backend.run_module_agent = module
    gen.generate_parent_module_docs = overview
    with pytest.raises(RuntimeError, match="parent: invalid parent response"):
        asyncio.run(gen.generate_module_documentation({}, []))
    assert (tmp_path / "independent.md").read_text() == VALID
