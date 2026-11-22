"""Recovery metadata describes this run's artifacts, not merely its settings."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
from codewiki.cli.commands.generate import _read_stored_language_state
from codewiki.cli.utils.errors import APIError
from codewiki.src.be.backend import UsageLedger
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.language import resolve_update_language


def _fail_generation(
    tmp_path, monkeypatch, entrypoint, produce, language=None, error_type=APIError, read_report=True
):
    """Fail through the real entrypoint and its metadata/usage cleanup."""
    if entrypoint == "cli":
        monkeypatch.setattr(
            CLIDocumentationGenerator, "_configure_backend_logging", lambda self: None
        )
        generator = CLIDocumentationGenerator(
            tmp_path,
            tmp_path,
            {
                "main_model": "m",
                "cluster_model": "m",
                "base_url": "http://unused",
                "api_key": "unused",
                "agent_instructions": {"language": language},
                "layout": "hierarchical",
            },
        )

        async def fail(config):
            generator._usage_ledger = UsageLedger()
            produce(config)
            raise error_type("injected generation failure")

        monkeypatch.setattr(generator, "_run_backend_generation", fail)
        with pytest.raises(error_type, match="injected generation failure"):
            generator.generate()
    else:
        generator = object.__new__(DocumentationGenerator)
        generator.config = SimpleNamespace(
            docs_dir=str(tmp_path), language=language, layout="hierarchical"
        )

        def fail():
            produce(generator.config)
            raise error_type("injected generation failure")

        generator.graph_builder = SimpleNamespace(build_dependency_graph=fail)
        with pytest.raises(error_type, match="injected generation failure"):
            asyncio.run(generator.run())
    if read_report:
        return json.loads((tmp_path / "metadata.json").read_text())


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
@pytest.mark.parametrize("old_tree", [False, True])
def test_early_failure_does_not_relabel_existing_spanish_docs(
    tmp_path, monkeypatch, entrypoint, old_tree
):
    page = tmp_path / "overview.md"
    page.write_text("# Resumen\nDocumentación en español.\n")
    if old_tree:
        (tmp_path / "module_tree.json").write_text("{}")
    (tmp_path / "metadata.json").write_text(json.dumps({"usage": {"incomplete": True}}))

    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, lambda config: None)

    assert "generation_info" not in metadata
    assert metadata["usage"]["incomplete"] is True
    assert _read_stored_language_state(tmp_path) == (False, None)
    assert resolve_update_language(None, "es", known=False) == "Spanish"
    assert "Documentación en español" in page.read_text()


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
def test_new_tree_in_empty_output_records_context(tmp_path, monkeypatch, entrypoint):
    def produce(config):
        (tmp_path / "module_tree.json").write_text("{}")

    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, produce, "Spanish")
    assert metadata["generation_info"] == {"language": "Spanish", "layout": "hierarchical"}


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
def test_new_tree_does_not_relabel_old_pages(tmp_path, monkeypatch, entrypoint):
    (tmp_path / "overview.md").write_text("# Resumen\nTexto español.\n")

    def produce(config):
        (tmp_path / "module_tree.json").write_text("{}")

    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, produce)
    assert "generation_info" not in metadata


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
@pytest.mark.parametrize("existing_page", [False, True])
def test_written_page_records_effective_context(tmp_path, monkeypatch, entrypoint, existing_page):
    if existing_page:
        (tmp_path / "overview.md").write_text("# Resumen anterior\n")

    def produce(config):
        config.layout = "flat"  # The updater can change the effective backend config.
        (tmp_path / "overview.md").write_text("# Resumen actualizado\nTexto español.\n")

    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, produce, "Spanish")
    assert metadata["generation_info"] == {"language": "Spanish", "layout": "flat"}


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
def test_rewriting_identical_old_page_does_not_establish_language(
    tmp_path, monkeypatch, entrypoint
):
    (tmp_path / "overview.md").write_text("# Resumen\n")

    def produce(config):
        (tmp_path / "overview.md").write_text("# Resumen\n")

    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, produce)
    assert "generation_info" not in metadata


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
@pytest.mark.parametrize(
    "info, expected",
    [
        (None, {"language": "Spanish", "layout": "hierarchical"}),
        ({}, {"language": "Spanish", "layout": "hierarchical"}),
        (
            {"commit_id": "abc"},
            {"commit_id": "abc", "language": "Spanish", "layout": "hierarchical"},
        ),
        ({"language": None}, {"language": None, "layout": "hierarchical"}),
        ({"layout": "flat"}, {"language": "Spanish", "layout": "flat"}),
        (
            {"language": "Japanese", "layout": "flat"},
            {"language": "Japanese", "layout": "flat"},
        ),
    ],
)
def test_partial_info_is_backfilled_without_overwriting_recorded_fields(
    tmp_path, monkeypatch, entrypoint, info, expected
):
    (tmp_path / "metadata.json").write_text(
        json.dumps({"generation_info": info, "update_history": [{"run_id": "old"}], "custom": 42})
    )
    (tmp_path / "module_tree.json").write_text("{}")

    def produce(config):
        (tmp_path / "overview.md").write_text("# Resumen\nTexto español.\n")

    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, produce, "Spanish")
    assert metadata["generation_info"] == expected
    assert metadata["update_history"] == [{"run_id": "old"}]
    assert metadata["custom"] == 42
    assert metadata["usage"]["incomplete"] is True


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
def test_early_failure_preserves_partial_info_as_unknown(tmp_path, monkeypatch, entrypoint):
    info = {"commit_id": "abc", "layout": "hierarchical"}
    (tmp_path / "metadata.json").write_text(json.dumps({"generation_info": info}))
    (tmp_path / "overview.md").write_text("# Resumen\n")
    metadata = _fail_generation(tmp_path, monkeypatch, entrypoint, lambda config: None, "Spanish")
    assert metadata["generation_info"] == info
    assert _read_stored_language_state(tmp_path) == (False, None)


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("artifact", [None, "module_tree.json", "overview.md"])
def test_interruption_saves_only_established_context(
    tmp_path, monkeypatch, entrypoint, error_type, artifact
):
    def produce(config):
        if artifact:
            (tmp_path / artifact).write_text("{}" if artifact.endswith(".json") else "# Resumen\n")

    metadata = _fail_generation(
        tmp_path, monkeypatch, entrypoint, produce, "Spanish", error_type=error_type
    )
    assert metadata["usage"]["incomplete"] is True
    if artifact:
        assert metadata["generation_info"] == {"language": "Spanish", "layout": "hierarchical"}
        assert resolve_update_language(_read_stored_language_state(tmp_path)[1], "es") == "Spanish"
    else:
        assert _read_stored_language_state(tmp_path) == (False, None)


@pytest.mark.parametrize("entrypoint", ["cli", "backend"])
def test_metadata_write_failure_does_not_replace_interruption(tmp_path, monkeypatch, entrypoint):
    from codewiki.cli.adapters import doc_generator
    from codewiki.src.be import documentation_generator

    def cannot_save(*args):
        raise OSError("disk full")

    monkeypatch.setattr(doc_generator, "save_metadata", cannot_save)
    monkeypatch.setattr(documentation_generator, "save_metadata", cannot_save)
    _fail_generation(
        tmp_path,
        monkeypatch,
        entrypoint,
        lambda config: None,
        error_type=KeyboardInterrupt,
        read_report=False,
    )


def test_cli_interrupt_exits_130_and_preserves_context(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from codewiki.cli.commands import generate
    from codewiki.cli.models.config import Configuration

    class Manager:
        def load(self):
            return True

        def is_configured(self):
            return True

        def get_config(self):
            return Configuration(base_url="", provider="codex", main_model="m", cluster_model="m")

        def get_api_key(self):
            return ""

    out = tmp_path / "docs"
    (tmp_path / "example.py").write_text("def f():\n    return 1\n")

    async def interrupt(self, config):
        self._usage_ledger = UsageLedger()
        out.mkdir()
        (out / "module_tree.json").write_text("{}")
        raise KeyboardInterrupt()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(generate, "ConfigManager", Manager)
    monkeypatch.setattr(CLIDocumentationGenerator, "_configure_backend_logging", lambda self: None)
    monkeypatch.setattr(CLIDocumentationGenerator, "_run_backend_generation", interrupt)
    result = CliRunner().invoke(generate.generate_command, ["-o", str(out), "--language", "es"])
    assert result.exit_code == 130, result.output
    assert "Interrupted by user" in result.output
    assert _read_stored_language_state(out) == (True, "Spanish")


def test_failed_atomic_metadata_write_preserves_previous_file(tmp_path, monkeypatch):
    from codewiki.src.be import generation_metadata

    path = tmp_path / "metadata.json"
    path.write_text('{"generation_info": {"language": "Spanish"}}')
    previous = path.read_bytes()

    def fail(data, stream, **kwargs):
        stream.write('{"partial":')
        raise OSError("disk full")

    monkeypatch.setattr(generation_metadata.json, "dump", fail)
    with pytest.raises(OSError, match="disk full"):
        generation_metadata.save_metadata(path, {"usage": {}})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]
