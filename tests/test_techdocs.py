"""Backstage TechDocs export (issue #145): CodeWiki docs -> mkdocs.yml + docs/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codewiki.cli.commands.techdocs import techdocs_command
from codewiki.cli.techdocs_generator import (
    MARKER_FILENAME,
    check_output_dir,
    generate_techdocs,
    normalize_repo_url,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPO = "https://github.com/acme/shop"
TREE = {
    "auth": {
        "components": ["src/auth.py::Auth", "src/auth.py::login"],
        "children": {
            "login": {"components": ["src/login.py::LoginForm"], "children": {}},
        },
    },
    "billing": {"components": ["src/billing.py"], "children": {}},
}


class _MkDocsLoader(yaml.SafeLoader):
    """Safe loader that accepts the ``!!python/name`` tag used for mermaid fences."""


_MkDocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_docs(root: Path, flat: bool = False) -> Path:
    docs = root / "docs"
    login_rel = "login.md" if flat else "auth/login.md"
    to_overview = "overview.md" if flat else "../overview.md"
    _write(
        docs / "overview.md",
        "# Shop\n\n**Shop** sells things. See [auth](auth.md) and [billing](billing.md).\n\n"
        "```mermaid\ngraph TD\n  A-->B\n```\n\n![diagram](arch.png)\n",
    )
    _write(
        docs / "auth.md",
        f"# Authentication\n\n- a bullet\n\nHandles users. Sub-page: [login]({login_rel}).\n"
        "Back to [overview](overview.md).\n",
    )
    _write(docs / login_rel, f"# Login\n\nThe login form. [Overview]({to_overview})\n")
    _write(docs / "billing.md", "# Billing\n\nCharges cards.\n")
    _write(docs / "module_tree.json", json.dumps(TREE))
    _write(
        docs / "metadata.json",
        json.dumps(
            {
                "generation_info": {
                    "timestamp": "2026-11-01T10:00:00+00:00",
                    "main_model": "test-model",
                    "repo_path": str(root / "shop"),
                    "commit_id": COMMIT,
                    "layout": "flat" if flat else "hierarchical",
                }
            }
        ),
    )
    _write(docs / "temp" / "dependency_graphs" / "g.json", "{}")
    (docs / "arch.png").write_bytes(b"\x89PNG")
    return docs


def _load_config(out: Path) -> dict:
    return yaml.load((out / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_MkDocsLoader)


def test_hierarchical_export(tmp_path):
    docs = _make_docs(tmp_path)
    out = tmp_path / "techdocs"

    result = generate_techdocs(docs, out, repo_url=REPO + ".git")

    assert result.pages == 4
    assert result.commit == COMMIT
    config = _load_config(out)
    assert config["plugins"] == ["techdocs-core"]
    assert config["docs_dir"] == "docs"
    assert config["repo_url"] == REPO
    assert config["site_name"] == "shop"
    assert config["site_description"] == "Shop sells things. See Authentication and Billing."
    assert config["nav"] == [
        {"Shop": "index.md"},
        {"Authentication": [{"Authentication": "auth.md"}, {"Login": "auth/login.md"}]},
        {"Billing": "billing.md"},
    ]
    fences = config["markdown_extensions"][0]["pymdownx.superfences"]["custom_fences"]
    assert fences[0]["class"] == "mermaid"

    out_docs = out / "docs"
    assert (out_docs / "index.md").is_file()
    assert not (out_docs / "overview.md").exists()
    assert (out_docs / "arch.png").read_bytes() == b"\x89PNG"
    assert not (out_docs / "module_tree.json").exists()
    assert not (out_docs / "metadata.json").exists()
    assert not (out_docs / "temp").exists()
    assert (out_docs / MARKER_FILENAME).is_file()

    # Links to the overview point at index.md, relative to each page
    assert "[Overview](../index.md)" in (out_docs / "auth/login.md").read_text()
    auth = (out_docs / "auth.md").read_text()
    assert "[Shop](index.md)" in auth
    # Links labelled with the module name show the page title
    assert "[Login](auth/login.md)" in auth

    # Front matter, provenance banner and component list
    assert auth.startswith(
        "---\ntitle: Authentication\ndescription: 'Handles users. Sub-page: Login."
    )
    assert f"[`{COMMIT[:8]}`]({REPO}/commit/{COMMIT})" in auth
    assert "on 2026-11-01 with `test-model`" in auth
    assert '??? abstract "Documented components (2)"' in auth
    assert f"[`login`]({REPO}/blob/{COMMIT}/src/auth.py) — `src/auth.py`" in auth
    billing = (out_docs / "billing.md").read_text()
    assert f"[`billing.py`]({REPO}/blob/{COMMIT}/src/billing.py)" in billing
    # The overview documents no components
    assert "Documented components" not in (out_docs / "index.md").read_text()

    # The source docs are untouched
    assert (docs / "overview.md").read_text().startswith("# Shop\n")


def test_flat_export(tmp_path):
    docs = _make_docs(tmp_path, flat=True)
    out = tmp_path / "techdocs"

    generate_techdocs(docs, out)

    config = _load_config(out)
    assert config["nav"][1] == {
        "Authentication": [{"Authentication": "auth.md"}, {"Login": "login.md"}]
    }
    assert "[Overview](index.md)" in (out / "docs/login.md").read_text()
    # No repository URL: components are listed without links
    assert "- `LoginForm` — `src/login.py`" in (out / "docs/login.md").read_text()
    assert "repo_url" not in config


def test_pages_outside_the_tree_are_added_to_nav(tmp_path):
    docs = _make_docs(tmp_path)
    _write(docs / "extra.md", "# Extra\n\nNot in the tree.\n")

    generate_techdocs(docs, tmp_path / "techdocs")

    assert _load_config(tmp_path / "techdocs")["nav"][-1] == {"Extra": "extra.md"}


def test_rerun_replaces_previous_export(tmp_path):
    docs = _make_docs(tmp_path)
    out = tmp_path / "techdocs"
    generate_techdocs(docs, out)
    (docs / "billing.md").unlink()

    generate_techdocs(docs, out)

    assert not (out / "docs/billing.md").exists()
    assert not (out / "docs/docs").exists()
    assert (out / "docs" / MARKER_FILENAME).is_file()


def test_refuses_foreign_docs_folder(tmp_path):
    docs = _make_docs(tmp_path)
    out = tmp_path / "techdocs"
    _write(out / "docs" / "handwritten.md", "# Mine\n")

    with pytest.raises(ValueError, match="not generated by CodeWiki"):
        generate_techdocs(docs, out)
    assert (out / "docs/handwritten.md").is_file()


@pytest.mark.parametrize("out", ["docs/techdocs", "."])
def test_refuses_overlapping_output(tmp_path, out):
    docs = _make_docs(tmp_path)
    with pytest.raises(ValueError, match="must be outside"):
        check_output_dir(tmp_path / out, docs)


def test_empty_docs_dir(tmp_path):
    (tmp_path / "docs").mkdir()
    with pytest.raises(FileNotFoundError):
        generate_techdocs(tmp_path / "docs", tmp_path / "techdocs")
    assert not (tmp_path / "techdocs").exists()


@pytest.mark.parametrize(
    "url, expected",
    [
        ("git@github.com:acme/shop.git", "https://github.com/acme/shop"),
        ("ssh://git@gitlab.example.com:2222/team/shop.git", "https://gitlab.example.com/team/shop"),
        ("https://user:token@github.com/acme/shop.git", "https://github.com/acme/shop"),
        ("https://github.com/acme/shop/", "https://github.com/acme/shop"),
        ("/srv/git/shop.git", None),
        (None, None),
    ],
)
def test_normalize_repo_url(url, expected):
    assert normalize_repo_url(url) == expected


def test_cli_command(tmp_path):
    docs = _make_docs(tmp_path)
    out = tmp_path / "td"
    runner = CliRunner()

    result = runner.invoke(
        techdocs_command,
        [str(docs), str(out), "--site-name", "Shop", "--repo-url", REPO],
    )

    assert result.exit_code == 0, result.output
    assert "Wrote 4 TechDocs pages" in result.output
    assert f"--etag {COMMIT}" in result.output
    assert _load_config(out)["site_name"] == "Shop"

    result = runner.invoke(techdocs_command, [str(docs), str(docs / "sub")])
    assert result.exit_code == 1
    assert "must be outside" in result.output
