"""
Post-generation instructions generator.
"""

from pathlib import Path
from typing import Optional
import click


def compute_github_pages_url(repo_url: str, repo_name: str) -> str:
    """
    Compute expected GitHub Pages URL from repository URL.

    Args:
        repo_url: GitHub repository URL
        repo_name: Repository name

    Returns:
        Expected GitHub Pages URL
    """
    # Extract owner from GitHub URL
    # e.g., "https://github.com/owner/repo" -> "owner"
    if "github.com" in repo_url:
        parts = repo_url.rstrip("/").split("/")
        if len(parts) >= 2:
            owner = parts[-2]
            repo = parts[-1].replace(".git", "")
            return f"https://{owner}.github.io/{repo}/"

    return f"https://YOUR_USERNAME.github.io/{repo_name}/"


def get_pr_creation_url(repo_url: str, branch_name: str) -> str:
    """
    Get PR creation URL for GitHub.

    Args:
        repo_url: GitHub repository URL
        branch_name: Branch name

    Returns:
        PR creation URL
    """
    base_url = repo_url.rstrip("/").replace(".git", "")
    return f"{base_url}/compare/{branch_name}"


def _compact(n: float) -> str:
    """1234567 -> '1.2M', 8900 -> '8.9k'."""
    for div, suffix in ((1_000_000, "M"), (1_000, "k")):
        if n >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{int(n)}"


def _echo_usage(statistics: dict) -> None:
    """Print the run's token usage and estimated cost, if any was recorded."""
    tokens = statistics.get("total_tokens_used") or 0
    if not tokens:
        return
    totals = statistics.get("usage_totals") or {}
    parts = [
        f"{label} {_compact(totals[key])}"
        for label, key in (
            ("in", "input_tokens"),
            ("out", "output_tokens"),
            ("cache-read", "cache_read_tokens"),
            ("cache-write", "cache_write_tokens"),
        )
        if totals.get(key)
    ]
    detail = f" ({' / '.join(parts)})" if parts else ""
    click.echo(f"  Tokens used:       {tokens:,}{detail}")

    cost = statistics.get("total_cost_usd")
    provider = statistics.get("provider") or ""
    if cost:
        note = (
            " (API-equivalent; billed to your subscription)"
            if provider in ("claude-code", "codex")
            else ""
        )
        click.echo(f"  Est. cost:         ${cost:,.2f}{note}")
    unpriced = statistics.get("unpriced_models") or []
    if unpriced:
        click.secho(f"  Cost unknown for:  {', '.join(unpriced)}", fg="yellow")
        if provider == "codex":
            # Claude Code reports its own cost; codex cost comes from caw's table
            click.echo(
                "    set a price: caw pricing set codex <model> --input <usd/1M> --output <usd/1M>"
            )


def display_post_generation_instructions(
    output_dir: Path,
    repo_name: str,
    repo_url: Optional[str] = None,
    branch_name: Optional[str] = None,
    github_pages: bool = False,
    files_generated: list = None,
    statistics: dict = None,
):
    """
    Display post-generation instructions.

    Args:
        output_dir: Output directory path
        repo_name: Repository name
        repo_url: GitHub repository URL (optional)
        branch_name: Git branch name (optional)
        github_pages: Whether GitHub Pages HTML was generated
        files_generated: List of generated files
        statistics: Generation statistics
    """
    click.echo()
    click.secho("✓ Documentation generated successfully!", fg="green", bold=True)
    click.echo()

    # Output directory
    click.secho("Output directory:", fg="cyan", bold=True)
    click.echo(f"  {output_dir}")
    click.echo()

    # Generated files
    if files_generated:
        click.secho("Generated files:", fg="cyan", bold=True)
        for file in files_generated[:10]:  # Show first 10
            click.echo(f"  - {file}")
        if len(files_generated) > 10:
            click.echo(f"  ... and {len(files_generated) - 10} more")
        click.echo()

    # Statistics
    if statistics:
        click.secho("Statistics:", fg="cyan", bold=True)
        if "module_count" in statistics:
            click.echo(f"  Total modules:     {statistics['module_count']}")
        if "total_files_analyzed" in statistics:
            click.echo(f"  Files analyzed:    {statistics['total_files_analyzed']}")
        if "generation_time" in statistics:
            minutes = int(statistics["generation_time"] // 60)
            seconds = int(statistics["generation_time"] % 60)
            click.echo(f"  Generation time:   {minutes} minutes {seconds} seconds")
        _echo_usage(statistics)
        click.echo()

    # Next steps
    click.secho("Next steps:", fg="cyan", bold=True)
    click.echo()

    click.echo("1. Review the generated documentation:")
    click.echo(f"   cat {output_dir}/overview.md")
    if github_pages:
        click.echo(f"   open {output_dir}/index.html  # View in browser")
    click.echo()

    if branch_name:
        # Git workflow with branch
        click.echo("2. Push the documentation branch:")
        click.secho(f"   git push origin {branch_name}", fg="yellow")
        click.echo()

        if repo_url:
            pr_url = get_pr_creation_url(repo_url, branch_name)
            click.echo("3. Create a Pull Request to merge documentation:")
            click.secho(f"   {pr_url}", fg="blue")
            click.echo()

            click.echo("4. After merge, enable GitHub Pages:")
        else:
            click.echo("3. Enable GitHub Pages:")
    else:
        # Direct commit workflow
        click.echo("2. Commit the documentation:")
        click.secho("   git add docs/", fg="yellow")
        click.secho('   git commit -m "Add generated documentation"', fg="yellow")
        click.echo()

        click.echo("3. Push to GitHub:")
        click.secho("   git push origin main", fg="yellow")
        click.echo()

        click.echo("4. Enable GitHub Pages:")

    click.echo("   - Go to repository Settings → Pages")
    click.echo("   - Source: Deploy from a branch")
    click.echo("   - Branch: main, folder: /docs")
    click.echo()

    if repo_url:
        github_pages_url = compute_github_pages_url(repo_url, repo_name)
        click.echo("5. Your documentation will be available at:")
        click.secho(f"   {github_pages_url}", fg="blue", bold=True)
        click.echo()


def display_generation_summary(
    success: bool, error_message: Optional[str] = None, output_dir: Optional[Path] = None
):
    """
    Display generation summary (success or failure).

    Args:
        success: Whether generation was successful
        error_message: Error message if failed
        output_dir: Output directory if successful
    """
    if success:
        click.echo()
        click.secho("✓ Generation completed successfully!", fg="green", bold=True)
        if output_dir:
            click.echo(f"\nDocumentation saved to: {output_dir}")
        click.echo()
    else:
        click.echo()
        click.secho("✗ Generation failed", fg="red", bold=True)
        if error_message:
            click.echo()
            click.echo(error_message)
        click.echo()
