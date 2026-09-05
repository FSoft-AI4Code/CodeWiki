"""Tests for leaf-node identifier filtering.

Covers filter_leaf_nodes: identifiers that merely contain a word like
"invalid" must survive, while strings that are actually error messages
reaching leaf-node selection instead of an identifier are dropped.
"""

from __future__ import annotations

from codewiki.src.be.dependency_analyzer.leaf_selection import filter_leaf_nodes
from codewiki.src.be.dependency_analyzer.models.core import Node


def _component(component_id: str, component_type: str = "function") -> Node:
    file_path = component_id.split("::")[0]
    return Node(
        id=component_id,
        name=component_id.split("::")[-1],
        component_type=component_type,
        file_path=file_path,
        relative_path=file_path,
    )


def _components(*component_ids: str) -> dict[str, Node]:
    return {cid: _component(cid) for cid in component_ids}


def test_identifiers_containing_error_words_are_kept() -> None:
    components = _components(
        "src/input.cpp::handleInvalidInput",
        "src/game.cpp::gameFailedCheck",
        "src/log.cpp::ErrorLog",
        "src/parser.cpp::parseExceptionTable",
    )

    kept = filter_leaf_nodes(list(components), components, {"function"})

    assert set(kept) == set(components)


def test_error_messages_are_dropped() -> None:
    components = _components("src/player.cpp::playerMove")
    candidates = [
        "src/player.cpp::playerMove",
        "Error: could not parse file",
        "invalid syntax at line 3",
        "Analysis failed for this component",
    ]

    kept = filter_leaf_nodes(candidates, components, {"function"})

    assert kept == ["src/player.cpp::playerMove"]


def test_unknown_and_malformed_entries_are_dropped() -> None:
    components = _components("src/player.cpp::playerMove")
    candidates = [
        "src/player.cpp::playerMove",
        "src/player.cpp::doesNotExist",
        "",
        "   ",
        None,
    ]

    kept = filter_leaf_nodes(candidates, components, {"function"})

    assert kept == ["src/player.cpp::playerMove"]


def test_components_of_other_types_are_dropped() -> None:
    components = {
        "src/game.cpp::runGame": _component("src/game.cpp::runGame", "function"),
        "src/game.h::GameState": _component("src/game.h::GameState", "struct"),
    }

    kept = filter_leaf_nodes(list(components), components, {"function"})

    assert kept == ["src/game.cpp::runGame"]
