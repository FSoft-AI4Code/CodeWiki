"""Tests for save_unwritten_doc: an agent that replies with the page instead of
writing it (seen with claude-code on itsdangerous' Timed_Serialization module,
which then failed generation with "module docs are missing")."""

from __future__ import annotations

from codewiki.src.be.caw_backend import save_unwritten_doc

PAGE = "# Timed Serialization\n\n## Overview\n\n" + "Timed tokens carry a timestamp. " * 30


def test_saves_final_reply_when_page_missing(tmp_path):
    path = tmp_path / "Serialization" / "Timed_Serialization.md"
    assert save_unwritten_doc(str(path), PAGE, "Timed_Serialization")
    assert path.read_text().startswith("# Timed Serialization")


def test_drops_preamble_before_first_heading(tmp_path):
    path = tmp_path / "Timed_Serialization.md"
    assert save_unwritten_doc(str(path), "Here is the documentation:\n\n" + PAGE, "Timed")
    assert path.read_text().startswith("# Timed Serialization")


def test_keeps_page_the_agent_wrote(tmp_path):
    path = tmp_path / "Timed_Serialization.md"
    path.write_text("# Written by the agent\n")
    assert not save_unwritten_doc(str(path), PAGE, "Timed_Serialization")
    assert path.read_text() == "# Written by the agent\n"


def test_ignores_status_replies(tmp_path):
    path = tmp_path / "Timed_Serialization.md"
    for reply in (None, "", "Done. I wrote the documentation.", "# Done\n\nWrote it."):
        assert not save_unwritten_doc(str(path), reply, "Timed_Serialization")
    assert not path.exists()
