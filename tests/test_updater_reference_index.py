"""Reference index extraction and inverse lookup."""

from __future__ import annotations

from updater_toy import REFRESH, graph_r1, tree_r1, write_pages

from codewiki.src.be.updater.reference_index import (
    build_reference_index,
    inverse,
    load_reference_index,
    save_reference_index,
)


def test_links_ids_names(tmp_path):
    write_pages(tmp_path)
    idx = build_reference_index(str(tmp_path), graph_r1(), tree_r1())
    assert idx["api"]["links"] == ["auth"]
    assert idx["api"]["ids"] == [REFRESH]
    assert "refresh" in idx["api"]["names"]
    assert idx["pipeline"]["links"] == ["auth"]
    assert "refresh" in idx["pipeline"]["names"]
    assert idx["overview"]["links"] == ["core", "pipeline", "storage"]
    inv = inverse(idx)
    assert inv["auth"] == {"api", "core", "pipeline"}
    assert inv[REFRESH] == {"api"}
    assert "User" in inv and inv["User"] == {"storage"}


def test_roundtrip(tmp_path):
    write_pages(tmp_path)
    idx = build_reference_index(str(tmp_path), graph_r1())
    save_reference_index(idx, str(tmp_path))
    assert load_reference_index(str(tmp_path)) == idx
