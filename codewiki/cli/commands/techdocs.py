"""
`codewiki techdocs`: export generated docs as a Backstage TechDocs source tree.
"""

import sys
from pathlib import Path

import click

from codewiki.cli.techdocs_generator import TechDocsResult, generate_techdocs


def echo_techdocs_next_steps(result: TechDocsResult) -> None:
    """How to build and register the exported TechDocs site."""
    click.echo("  Build it:")
    etag = f" --etag {result.commit}" if result.commit else ""
    click.secho(f"    techdocs-cli generate --source-dir {result.out_dir}{etag}", fg="yellow")
    click.echo("  Or point the catalog entity at it (catalog-info.yaml):")
    click.secho(f"    backstage.io/techdocs-ref: dir:./{result.out_dir.name}", fg="yellow")


@click.command(name="techdocs")
@click.argument(
    "docs_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default="docs",
)
@click.argument(
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default="techdocs",
)
@click.option("--site-name", default=None, help="Site name (default: repository folder name)")
@click.option("--repo-url", default=None, help="https repository URL (default: git origin)")
@click.option(
    "--source-url-template",
    default=None,
    help="Source link format with {repo}, {ref} and {path}, e.g. for Bitbucket Server: "
    "'https://bb.example.com/projects/P/repos/R/browse/{path}?at={ref}'. "
    "GitHub, GitLab and bitbucket.org are detected automatically.",
)
def techdocs_command(
    docs_dir: Path,
    out_dir: Path,
    site_name: str | None,
    repo_url: str | None,
    source_url_template: str | None,
):
    """Export CodeWiki docs as a Backstage TechDocs (MkDocs) project.

    Reads the pages in DOCS_DIR (default: ./docs) and writes mkdocs.yml and
    docs/ to OUT_DIR (default: ./techdocs). No LLM is called, so run it again
    after every `codewiki generate --update`. The source docs are not changed.

    \b
    Examples:
      $ codewiki techdocs
      $ codewiki techdocs docs techdocs --site-name "Payments API"
    """
    docs_dir = docs_dir.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    try:
        result = generate_techdocs(
            docs_dir,
            out_dir,
            site_name=site_name,
            repo_url=repo_url,
            source_template=source_url_template,
        )
    except (FileNotFoundError, ValueError) as e:
        click.secho(f"✗ {e}", fg="red", err=True)
        sys.exit(1)

    click.secho(f"✓ Wrote {result.pages} TechDocs pages to {result.out_dir}", fg="green")
    echo_techdocs_next_steps(result)
