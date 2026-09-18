"""Tests for the CLI owl banner."""

import io

import click
import pytest
from click.testing import CliRunner

from codewiki import __version__
from codewiki.cli.main import cli
from codewiki.cli.utils import branding


def test_render_logo_matches_art_when_unstyled():
    lines = branding.render_logo()
    assert len(lines) == 20
    plain = [click.unstyle(line).rstrip() for line in lines]
    assert plain == branding.OWL_ART.split("\n")


def test_owl_art_is_mirror_symmetric():
    width = max(len(line) for line in branding.OWL_ART.split("\n"))
    for line in branding.OWL_ART.split("\n"):
        padded = line.ljust(width)
        assert padded == padded[::-1], line


def test_side_by_side_fits_80_columns():
    banner = click.unstyle(branding.build_banner("9.9.9", columns=80))
    assert any("%" in line and "CodeWiki" in line for line in banner.splitlines())
    assert max(len(line) for line in banner.splitlines()) <= 80


def test_render_logo_colors_body_and_accents(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    joined = "".join(branding.render_logo())
    assert "\x1b[1m" in joined  # bold body
    assert f"\x1b[38;5;{branding.ORANGE}m" in joined  # orange accents


def test_no_color_disables_styling(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b[" not in "".join(branding.render_logo())
    assert "\x1b[" not in "".join(branding.render_wordmark("1.0"))


def test_should_show_banner_respects_env_and_tty(monkeypatch):
    monkeypatch.delenv("CODEWIKI_NO_BANNER", raising=False)
    assert branding.should_show_banner(io.StringIO()) is False

    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert branding.should_show_banner(Tty()) is True
    monkeypatch.setenv("CODEWIKI_NO_BANNER", "1")
    assert branding.should_show_banner(Tty()) is False


@pytest.mark.parametrize("columns", [120, 60])
def test_build_banner_layouts(columns):
    banner = click.unstyle(branding.build_banner("9.9.9", columns=columns))
    assert "CodeWiki" in banner
    assert "v9.9.9" in banner
    assert branding.TAGLINE in banner
    if columns >= 80:
        # Wordmark sits beside the owl on a shared line.
        assert any("%" in line and "CodeWiki" in line for line in banner.splitlines())
    else:
        # Wordmark is stacked below the owl.
        assert not any("%" in line and "CodeWiki" in line for line in banner.splitlines())
        assert max(len(line) for line in banner.splitlines()) <= branding._ART_WIDTH


def test_version_command_prints_banner_even_when_piped(monkeypatch):
    monkeypatch.delenv("CODEWIKI_NO_BANNER", raising=False)
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "%%%" in result.output
    assert f"CodeWiki CLI v{__version__}" in result.output
    assert "\x1b[" not in result.output  # click strips colors off-TTY


def test_version_command_honors_no_banner(monkeypatch):
    monkeypatch.setenv("CODEWIKI_NO_BANNER", "1")
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "%%%" not in result.output
    assert result.output.startswith(f"CodeWiki CLI v{__version__}")


def test_bare_cli_prints_help_and_exits_zero():
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "%%%" not in result.output  # not a TTY


@pytest.mark.parametrize("args", [["--help"], ["generate", "--help"], ["config", "set", "--help"]])
def test_help_output_has_no_banner(args):
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0
    assert "%%%" not in result.output
    assert "Usage:" in result.output
