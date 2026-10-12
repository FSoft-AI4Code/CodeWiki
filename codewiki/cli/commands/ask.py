"""``codewiki ask``: answer a question from the generated docs + code in the terminal."""

from __future__ import annotations

import json
import sys

import click


@click.command(name="ask")
@click.argument("question", nargs=-1, required=True)
@click.option(
    "--docs",
    "docs_dir",
    default=None,
    help="Generated docs folder (default: $CODEWIKI_DOCS_DIR, else ./docs)",
)
@click.option("--session", "session_id", default=None, help="Session id from an earlier answer")
@click.option(
    "--max-context-tokens", type=click.IntRange(min=500), default=None, help="Cap on context"
)
@click.option("--model", default=None, help="Model for the answer (default: main_model)")
@click.option("--json", "as_json", is_flag=True, help="Print the full result as JSON")
@click.option("--no-log-questions", is_flag=True, help="Keep question text out of the query log")
def ask_command(
    question, docs_dir, session_id, max_context_tokens, model, as_json, no_log_questions
):
    """Ask a question about the codebase, answered from its docs and code.

    \b
    Examples:
      codewiki ask "where is the signature verified?"
      codewiki ask "what breaks if I remove max_age from TimestampSigner.unsign?"
    """
    from codewiki.src.be.qa import DocsNotFoundError, get_engine

    text = " ".join(question).strip()
    try:
        engine = get_engine(docs_dir, model=model, log_questions=not no_log_questions)
    except (DocsNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e

    def on_event(event: dict) -> None:
        if not as_json:
            click.secho(f"… {event.get('message', '')}", fg="bright_black", err=True)

    result = engine.ask(
        text, session_id=session_id, max_context_tokens=max_context_tokens, on_event=on_event
    )
    if as_json:
        json.dump(result.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
        click.echo()
        return
    click.echo(result.answer.strip())
    if result.sources:
        click.echo()
        click.secho("Sources:", bold=True)
        for src in result.sources:
            stale = " (stale doc)" if src.get("stale") else ""
            click.echo(f"  {src['location']}{stale}")
    click.echo()
    click.secho(result.stats.footer(), fg="cyan")
    click.secho(
        f"{result.freshness} · follow up with --session {result.session_id}", fg="bright_black"
    )
