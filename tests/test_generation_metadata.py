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


def _fail_generation(tmp_path, monkeypatch, entrypoint, produce, language=None):
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
            raise APIError("injected generation failure")

        monkeypatch.setattr(generator, "_run_backend_generation", fail)
        with pytest.raises(APIError, match="injected generation failure"):
            generator.generate()
    else:
        generator = object.__new__(DocumentationGenerator)
        generator.config = SimpleNamespace(
            docs_dir=str(tmp_path), language=language, layout="hierarchical"
        )

        def fail():
            produce(generator.config)
            raise APIError("injected generation failure")

        generator.graph_builder = SimpleNamespace(build_dependency_graph=fail)
        with pytest.raises(APIError, match="injected generation failure"):
            asyncio.run(generator.run())
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
