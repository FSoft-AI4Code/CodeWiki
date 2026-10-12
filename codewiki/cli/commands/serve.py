"""``codewiki serve``: browse the docs and chat with them, locally."""

from __future__ import annotations

import logging
import sys

import click

from codewiki.src.config import DEFAULT_QA_PORT


@click.command(name="serve")
@click.option(
    "--docs",
    "docs_dir",
    default=None,
    help="Generated docs folder (default: $CODEWIKI_DOCS_DIR, else ./docs)",
)
@click.option("--port", type=int, default=DEFAULT_QA_PORT, show_default=True)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to listen on. Anything but localhost exposes your code and LLM quota.",
)
@click.option(
    "--allow-origin",
    "allow_origins",
    multiple=True,
    help="Extra site origin allowed to use the chat API (repeatable), e.g. a custom Pages domain",
)
@click.option("--model", default=None, help="Model for answers (default: main_model)")
@click.option("--no-log-questions", is_flag=True, help="Keep question text out of the query log")
def serve_command(docs_dir, port, host, allow_origins, model, no_log_questions):
    """Serve the docs site with a local "Ask AI" chat over the docs and code.

    Open http://127.0.0.1:8765, or keep using the GitHub Pages site: its chat
    window finds this server on your machine. Answers use your CodeWiki
    config (`codewiki config set`); nothing leaves your machine except the
    LLM calls themselves.
    """
    from codewiki.src.be.qa import DocsNotFoundError, get_index, resolve_docs_dir
    from codewiki.src.be.qa.server import default_origins, origin_of, serve

    try:
        docs = resolve_docs_dir(docs_dir)
    except DocsNotFoundError as e:
        raise click.ClickException(str(e)) from e
    if host not in ("127.0.0.1", "localhost", "::1"):
        click.secho(
            f"Warning: listening on {host} lets other machines use this API "
            "(your code and your LLM quota).",
            fg="yellow",
            err=True,
        )
    index = get_index(docs)
    origins = sorted(default_origins(host, port, docs) | {origin_of(o) or o for o in allow_origins})
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    qa_logger = logging.getLogger("codewiki.src.be.qa")
    qa_logger.addHandler(handler)
    qa_logger.setLevel(logging.INFO)

    click.secho(f"CodeWiki docs + chat: http://{host}:{port}/", fg="green", bold=True)
    click.echo(f"  Docs:      {docs}")
    click.echo(
        f"  Indexed:   {len(index.sections)} doc sections, {len(index.code)} code components"
    )
    click.echo(f"  Freshness: {index.freshness.summary()}")
    if index.stale_pages:
        click.secho(
            f"  {len(index.stale_pages)} page(s) are older than the code; answers use the "
            "current code. Refresh them with `codewiki generate --update`.",
            fg="yellow",
        )
    click.echo(f"  Chat allowed from: {', '.join(origins)}")
    click.echo("  Ctrl+C to stop.")
    serve(docs, host, port, allow_origins, model=model, log_questions=not no_log_questions)
