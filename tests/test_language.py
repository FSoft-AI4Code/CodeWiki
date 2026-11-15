"""Output language (--language): prompts, config plumbing, metadata, viewer titles."""

import json
from types import SimpleNamespace

import pytest

from codewiki.cli.commands.generate import (
    _read_stored_language,
    _read_stored_language_state,
)
from codewiki.cli.html_generator import HTMLGenerator
from codewiki.cli.models.config import AgentInstructions, Configuration
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.module_naming import extract_page_titles
from codewiki.src.be.updater.prompts import (
    STALE_FIX_SYSTEM_PROMPT,
    format_stale_fix_system_prompt,
)
from codewiki.src.config import Config
from codewiki.src.language import (
    format_language_directive,
    language_tag,
    normalize_language,
    resolve_update_language,
)


def _backend_config(agent_instructions):
    return Config.from_cli(
        repo_path=".",
        output_dir="/tmp/out",
        llm_base_url="http://x",
        llm_api_key="k",
        main_model="m",
        cluster_model="m",
        agent_instructions=agent_instructions,
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ja", "Japanese"),
        ("JA", "Japanese"),
        ("Japanese", "Japanese"),
        ("zh_CN", "Chinese (Simplified)"),
        ("vi", "Vietnamese"),
        ("Klingon", "Klingon"),
        ("en", None),
        ("English", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_language(value, expected):
    assert normalize_language(value) == expected


def test_language_tag():
    assert language_tag("Japanese") == "ja"
    assert language_tag("zh") == "zh-CN"
    assert language_tag("Klingon") == "en"
    assert language_tag(None) == "en"


def test_prompt_addition_puts_language_first_and_keeps_filenames():
    cfg = _backend_config({"language": "ja", "custom_instructions": "Be brief."})
    addition = cfg.get_prompt_addition()
    assert addition.startswith("<OUTPUT_LANGUAGE>")
    assert "in Japanese" in addition
    assert "never translate or rename them" in addition
    assert addition.endswith("Additional instructions: Be brief.")
    assert cfg.language == "Japanese"


def test_english_leaves_prompts_unchanged():
    assert format_language_directive("en") == ""
    assert _backend_config({"language": "en"}).get_prompt_addition() == ""
    assert _backend_config({"custom_instructions": "x"}).get_prompt_addition() == (
        "Additional instructions: x"
    )


def test_agent_instructions_round_trip_and_merge():
    ai = AgentInstructions(language="ja")
    assert not ai.is_empty()
    assert AgentInstructions.from_dict(ai.to_dict()).language == "ja"

    saved = AgentInstructions(language="vi", custom_instructions="saved")
    merged = AgentInstructions(doc_type="api").merged_with(saved)
    assert (merged.language, merged.custom_instructions, merged.doc_type) == (
        "vi",
        "saved",
        "api",
    )
    assert AgentInstructions(language="ja").merged_with(saved).language == "ja"


def test_to_backend_config_carries_language_and_artifact_exclude():
    cfg = Configuration.from_dict(
        {
            "base_url": "http://x",
            "main_model": "m",
            "cluster_model": "m",
            "agent_instructions": {"language": "ko", "artifact_exclude": ["gen/*"]},
        }
    )
    backend = cfg.to_backend_config(
        repo_path=".",
        output_dir="/tmp/out",
        api_key="k",
        runtime_instructions=AgentInstructions(doc_type="api"),
    )
    assert backend.language == "Korean"
    assert backend.artifact_exclude == ["gen/*"]


def test_metadata_records_language(tmp_path):
    gen = object.__new__(DocumentationGenerator)
    gen.config = _backend_config({"language": "ja"})
    gen.commit_id = "abc"
    gen.create_documentation_metadata(str(tmp_path), {}, 0)
    info = json.loads((tmp_path / "metadata.json").read_text())["generation_info"]
    assert info["language"] == "Japanese"
    assert _read_stored_language(tmp_path) == "Japanese"


def test_read_stored_language_without_metadata(tmp_path):
    assert _read_stored_language(tmp_path) is None


def test_resolve_update_language():
    assert resolve_update_language("Japanese", None) == "Japanese"
    assert resolve_update_language("Japanese", "ja") == "Japanese"
    assert resolve_update_language(None, None) is None
    assert resolve_update_language(None, "en") is None
    with pytest.raises(ValueError, match="rerun without --update"):
        resolve_update_language("Japanese", "vi")
    with pytest.raises(ValueError, match="English"):
        resolve_update_language(None, "ja")


def test_stale_fix_prompt_gets_instructions():
    assert format_stale_fix_system_prompt(None) == STALE_FIX_SYSTEM_PROMPT
    prompt = format_stale_fix_system_prompt(format_language_directive("ja"))
    assert prompt.startswith(STALE_FIX_SYSTEM_PROMPT)
    assert "in Japanese" in prompt


def _write_docs(tmp_path, language):
    tree = {"auth": {"components": ["a"], "children": {"auth_tokens": {"components": ["b"]}}}}
    (tmp_path / "module_tree.json").write_text(json.dumps(tree))
    (tmp_path / "overview.md").write_text("# 概要\n\ntext\n", encoding="utf-8")
    (tmp_path / "auth.md").write_text("```python\n# not a title\n```\n# 認証 #\n", encoding="utf-8")
    (tmp_path / "auth_tokens.md").write_text("no heading\n", encoding="utf-8")
    (tmp_path / "metadata.json").write_text(json.dumps({"generation_info": {"language": language}}))
    return tree


def test_extract_page_titles(tmp_path):
    tree = _write_docs(tmp_path, "Japanese")
    assert extract_page_titles(str(tmp_path), tree) == {"overview": "概要", "auth": "認証"}


def test_viewer_uses_translated_titles_and_lang(tmp_path):
    _write_docs(tmp_path, "Japanese")
    out = tmp_path / "index.html"
    HTMLGenerator().generate(output_path=out, title="repo", docs_dir=tmp_path)
    html = out.read_text(encoding="utf-8")
    assert '<html lang="ja">' in html
    assert '"auth": "認証"' in html
    assert "{{" not in html.split("<script>")[0]


def test_viewer_default_is_english_without_titles(tmp_path):
    _write_docs(tmp_path, None)
    out = tmp_path / "index.html"
    HTMLGenerator().generate(output_path=out, title="repo", docs_dir=tmp_path)
    html = out.read_text(encoding="utf-8")
    assert '<html lang="en">' in html
    assert "const PAGE_TITLES = {};" in html


def test_metadata_tolerates_config_stub_without_language(tmp_path):
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(main_model="m", repo_path=".", max_depth=2)
    gen.commit_id = None
    gen.create_documentation_metadata(str(tmp_path), {}, 0)
    assert _read_stored_language(tmp_path) is None


def test_read_stored_language_state_distinguishes_recorded_vs_missing(tmp_path):
    assert _read_stored_language_state(tmp_path) == (False, None)

    (tmp_path / "metadata.json").write_text(json.dumps({"usage": {"incomplete": True}}))
    assert _read_stored_language_state(tmp_path) == (False, None)

    (tmp_path / "metadata.json").write_text(json.dumps({"generation_info": {"commit_id": "abc"}}))
    assert _read_stored_language_state(tmp_path) == (False, None)

    (tmp_path / "metadata.json").write_text(json.dumps({"generation_info": {"language": None}}))
    assert _read_stored_language_state(tmp_path) == (True, None)

    (tmp_path / "metadata.json").write_text(
        json.dumps({"generation_info": {"language": "Spanish"}})
    )
    assert _read_stored_language_state(tmp_path) == (True, "Spanish")


def test_resolve_update_language_unknown_metadata():
    assert resolve_update_language(None, "es", known=False) == "Spanish"
    assert resolve_update_language(None, "en", known=False) is None
    assert resolve_update_language(None, None, known=False) is None


def test_generate_update_language_handling(tmp_path, monkeypatch):
    from click.testing import CliRunner
    import codewiki.cli.commands.generate as gen

    class Stop(Exception):
        pass

    seen = {}
    saved_agent = AgentInstructions()
    cfg = SimpleNamespace(
        main_model="m",
        cluster_model="m",
        fallback_model="m",
        base_url="http://x",
        provider="openai-compatible",
        aws_region="us-east-1",
        agent_instructions=saved_agent,
        max_tokens=1,
        min_token_per_module=1000,
        max_token_per_module=1,
        max_token_per_leaf_module=1,
        max_depth=1,
        request_limit=1,
        agent_retries=1,
        use_gitignore=True,
        prompt_caching=False,
    )

    class FakeManager:
        def load(self):
            return True

        def is_configured(self):
            return True

        def get_config(self):
            return cfg

        def get_api_key(self):
            return "k"

    def fake_generator(**kwargs):
        seen.clear()
        seen.update(kwargs["config"])
        raise Stop

    monkeypatch.setattr(gen, "ConfigManager", FakeManager)
    monkeypatch.setattr(gen, "CLIDocumentationGenerator", fake_generator)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(tmp_path)

    # Incomplete metadata (no generation_info): --update --language es is accepted
    (out / "metadata.json").write_text(json.dumps({"usage": {"incomplete": True}}))
    CliRunner().invoke(gen.generate_command, ["--update", "--language", "es", "-o", str(out)])
    assert seen["agent_instructions"]["language"] == "Spanish"

    # Metadata without language key: saved config language is preserved when --language omitted
    saved_agent.language = "es"
    (out / "metadata.json").write_text(json.dumps({"generation_info": {"commit_id": "abc"}}))
    CliRunner().invoke(gen.generate_command, ["--update", "-o", str(out)])
    assert seen["agent_instructions"]["language"] == "es"
    saved_agent.language = None

    # Explicit English metadata still rejects --update --language es
    (out / "metadata.json").write_text(json.dumps({"generation_info": {"language": None}}))
    res = CliRunner().invoke(
        gen.generate_command, ["--update", "--language", "es", "-o", str(out)]
    )
    assert res.exit_code != 0
    assert "generated in English, but --language asks for Spanish" in res.output

    # Explicit Spanish metadata accepts --language es and rejects --language fr
    (out / "metadata.json").write_text(json.dumps({"generation_info": {"language": "Spanish"}}))
    CliRunner().invoke(gen.generate_command, ["--update", "--language", "es", "-o", str(out)])
    assert seen["agent_instructions"]["language"] == "Spanish"

    res = CliRunner().invoke(
        gen.generate_command, ["--update", "--language", "fr", "-o", str(out)]
    )
    assert res.exit_code != 0
    assert "generated in Spanish, but --language asks for French" in res.output


def test_incomplete_run_records_language_and_layout(tmp_path, monkeypatch):
    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
    from codewiki.cli.utils.errors import APIError
    from codewiki.src.be.backend import UsageLedger

    out = tmp_path / "docs"
    out.mkdir()
    (out / "module_tree.json").write_text("{}")

    instance = CLIDocumentationGenerator(
        tmp_path,
        out,
        {
            "main_model": "m",
            "cluster_model": "m",
            "base_url": "http://x",
            "api_key": "k",
            "agent_instructions": {"language": "es"},
            "layout": "hierarchical",
        },
    )

    async def fail(cfg):
        instance._usage_ledger = UsageLedger()
        raise APIError("module failed")

    monkeypatch.setattr(instance, "_run_backend_generation", fail)
    with pytest.raises(APIError):
        instance.generate()

    metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["usage"]["incomplete"] is True
    assert metadata["generation_info"] == {"language": "Spanish", "layout": "hierarchical"}
