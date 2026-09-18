"""
CLI branding: the CodeWiki owl banner.
"""

import os
import shutil
import sys
from typing import List, Optional

import click

OWL_ART = """\
 %%                                     %%
%%%%%                                 %%%%%
%%%%%%%%        %%%%%%%%%%%        %%%%%%%%
 %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
  %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
   %%   %%%%%%%%%%%%%%%%%%%%%%%%%%%   %%
    %%%%      %%%%%%%%%%%%%%%      %%%%
  %%%%%%         %%%%%%%%%         %%%%%%
 %%%%%     ====   %%%%%%%   ====     %%%%%
 %%%%    =====     %%%%%     =====    %%%%
%%%%%  ======      %%%%%      ======  %%%%%
%%%%%   =====      %%%%%      =====   %%%%%
%%%%%     =====   %%%=%%%   =====     %%%%%
 %%%%%%     ==   %%=====%%   ==     %%%%%%
  %%%%%%%%    %%%%=======%%%%    %%%%%%%%
   %%%%%%%%%%%%%%%%=====%%%%%%%%%%%%%%%%
     %%%%%%%%%%%%%%%===%%%%%%%%%%%%%%%
       %%%%%%%%%%%%%%=%%%%%%%%%%%%%%
            %%%%%%%%%%%%%%%%%%%
                  %%%%%%%"""

# 256-color index closest to the brand orange (#ff7a00).
ORANGE = 208

BODY_CHARS = "%@"
ACCENT_CHARS = "=+#*"

TAGLINE = "AI agents document your codebase"

_ART_LINES = OWL_ART.split("\n")
_ART_WIDTH = max(len(line) for line in _ART_LINES)
_GUTTER = "  "


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _colors_enabled() -> bool:
    # https://no-color.org/ - any non-empty value disables color.
    return not os.environ.get("NO_COLOR")


def _style(text: str, **kwargs) -> str:
    if not text or not _colors_enabled():
        return text
    return click.style(text, **kwargs)


def should_show_banner(stream=None) -> bool:
    """
    Decide whether the banner should be printed.

    The banner is skipped when CODEWIKI_NO_BANNER is set or when the output
    stream is not an interactive terminal (pipes, CI logs, CliRunner).
    """
    if _env_flag("CODEWIKI_NO_BANNER"):
        return False
    stream = stream if stream is not None else sys.stdout
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def render_logo() -> List[str]:
    """Return the owl as styled lines, each padded to the art width."""
    styled_lines = []
    for line in _ART_LINES:
        padded = line.ljust(_ART_WIDTH)
        out = []
        run = ""
        run_kind = None
        for ch in padded:
            kind = "body" if ch in BODY_CHARS else "accent" if ch in ACCENT_CHARS else "blank"
            if kind != run_kind and run:
                out.append(_style_run(run, run_kind))
                run = ""
            run += ch
            run_kind = kind
        if run:
            out.append(_style_run(run, run_kind))
        styled_lines.append("".join(out))
    return styled_lines


def _style_run(run: str, kind: Optional[str]) -> str:
    if kind == "body":
        return _style(run, bold=True)
    if kind == "accent":
        return _style(run, fg=ORANGE)
    return run


def render_wordmark(version: str) -> List[str]:
    """Return the 'CodeWiki' wordmark, version and tagline as styled lines."""
    name = _style("Code", bold=True) + _style("Wiki", fg=ORANGE, bold=True)
    return [
        name,
        _style(f"v{version}", dim=True),
        _style(TAGLINE, dim=True),
    ]


def build_banner(version: str, columns: Optional[int] = None) -> str:
    """
    Compose the banner.

    When the terminal is wide enough for the owl, a gutter and the longest
    wordmark line (79 columns), the wordmark sits to the right of the owl,
    vertically centered. On narrower terminals it is stacked underneath.
    """
    if columns is None:
        columns = shutil.get_terminal_size((80, 24)).columns

    logo = render_logo()
    wordmark = render_wordmark(version)

    widest_text = max(len(click.unstyle(text)) for text in wordmark)
    if columns >= _ART_WIDTH + len(_GUTTER) + widest_text:
        start = (len(logo) - len(wordmark)) // 2
        lines = []
        for i, art_line in enumerate(logo):
            j = i - start
            if 0 <= j < len(wordmark):
                lines.append(f"{art_line}{_GUTTER}{wordmark[j]}")
            else:
                lines.append(art_line.rstrip())
    else:
        lines = [line.rstrip() for line in logo]
        lines.append("")
        for text in wordmark:
            visible = len(click.unstyle(text))
            pad = max((_ART_WIDTH - visible) // 2, 0)
            lines.append(" " * pad + text)

    return "\n" + "\n".join(lines) + "\n"


def print_banner(version: Optional[str] = None, force: bool = False) -> None:
    """
    Print the banner to stdout.

    Args:
        version: Version string to show; defaults to the package version.
        force: Print even when stdout is not a TTY. CODEWIKI_NO_BANNER is
            still honored.
    """
    if _env_flag("CODEWIKI_NO_BANNER"):
        return
    if not force and not should_show_banner():
        return
    if version is None:
        from codewiki import __version__ as version

    click.echo(build_banner(version))
