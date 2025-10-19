"""
Main CLI application for CodeWiki using Click framework.
"""

import sys
import click
from pathlib import Path

from codewiki import __version__


@click.group()
@click.version_option(version=__version__, prog_name="CodeWiki CLI")
@click.pass_context
def cli(ctx):
    """
    CodeWiki: Transform codebases into comprehensive documentation.
    
    Generate AI-powered documentation for your code repositories with support
    for Python, Java, JavaScript, TypeScript, C, C++, and C#.
    """
    # Ensure context object exists
    ctx.ensure_object(dict)


@cli.command()
def version():
    """Display version information."""
    click.echo(f"CodeWiki CLI v{__version__}")
    click.echo("Python-based documentation generator using AI analysis")
    

# Import commands
from codewiki.cli.commands.config import config_group
from codewiki.cli.commands.generate import generate_command

# Register command groups
cli.add_command(config_group)
cli.add_command(generate_command, name="generate")


def main():
    """Entry point for the CLI."""
    try:
        cli(obj={})
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted by user", err=True)
        sys.exit(130)
    except Exception as e:
        click.secho(f"\n✗ Unexpected error: {e}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

