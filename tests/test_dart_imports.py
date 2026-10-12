"""Tests for Dart directive parsing, URI resolution and visibility scopes."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from tree_sitter_language_pack import get_parser

from codewiki.src.be.dependency_analyzer.analyzers.dart_imports import (
    DartDirective,
    DartPackageResolver,
    build_scopes,
    is_private_dart_name,
    parse_directives,
)


def _directives(source: str) -> list[DartDirective]:
    return parse_directives(get_parser("dart").parse(source.encode()).root_node)


def test_parse_directives_covers_all_forms() -> None:
    source = (
        "import 'package:flutter/material.dart';\n"
        "import 'package:demo/models/car.dart' as car show Car;\n"
        "import '../services/api.dart' hide Foo;\n"
        "export 'src/widgets.dart';\n"
        "part 'home.g.dart';\n"
    )
    assert _directives(source) == [
        DartDirective("import", "package:flutter/material.dart", None, 1),
        DartDirective("import", "package:demo/models/car.dart", "car", 2),
        DartDirective("import", "../services/api.dart", None, 3),
        DartDirective("export", "src/widgets.dart", None, 4),
        DartDirective("part", "home.g.dart", None, 5),
    ]


def test_parse_part_of_uri_and_library_name_forms() -> None:
    assert _directives("part of 'home.dart';\n") == [DartDirective("part_of", "home.dart", None, 1)]
    assert _directives("part of my.lib;\n") == [DartDirective("part_of", None, None, 1)]


def test_package_resolver_maps_own_package_relative_and_rejects_others(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    resolver = DartPackageResolver.from_files(str(tmp_path), ["lib/a/home.dart"])
    assert resolver.packages == {"demo": ""}
    assert (
        resolver.resolve("package:demo/models/car.dart", "lib/a/home.dart") == "lib/models/car.dart"
    )
    assert resolver.resolve("../b/x.dart", "lib/a/home.dart") == "lib/b/x.dart"
    assert resolver.resolve("x.dart", "lib/a/home.dart") == "lib/a/x.dart"
    assert resolver.resolve("package:flutter/material.dart", "lib/a/home.dart") is None
    assert resolver.resolve("dart:async", "lib/a/home.dart") is None
    assert resolver.resolve("../../../escape.dart", "lib/a/home.dart") is None
    assert resolver.resolve(None, "lib/a/home.dart") is None


def test_package_resolver_handles_nested_packages(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: root_app\n", encoding="utf-8")
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "pubspec.yaml").write_text("name: 'core'\n", encoding="utf-8")
    resolver = DartPackageResolver.from_files(
        str(tmp_path), ["lib/main.dart", "packages/core/lib/src/util.dart"]
    )
    assert resolver.packages == {"root_app": "", "core": "packages/core"}
    assert resolver.resolve("package:core/src/util.dart", "lib/main.dart") == (
        "packages/core/lib/src/util.dart"
    )


def test_build_scopes_follows_imports_parts_and_exports() -> None:
    resolver = DartPackageResolver({"demo": ""})
    directives = {
        "lib/home.dart": [
            DartDirective("import", "package:demo/barrel.dart"),
            DartDirective("part", "home_part.dart"),
        ],
        "lib/home_part.dart": [DartDirective("part_of", "home.dart")],
        "lib/barrel.dart": [DartDirective("export", "src/card.dart")],
        "lib/src/card.dart": [DartDirective("export", "deep.dart")],
        "lib/src/deep.dart": [],
        "lib/other.dart": [],
    }
    scopes = build_scopes(directives, resolver, set(directives))
    expected = {
        "lib/home.dart",
        "lib/home_part.dart",
        "lib/barrel.dart",
        "lib/src/card.dart",
        "lib/src/deep.dart",
    }
    assert scopes.visible["lib/home.dart"] == expected
    # A part sees exactly what its library sees.
    assert scopes.visible["lib/home_part.dart"] == expected
    assert scopes.library_members["lib/home_part.dart"] == {"lib/home.dart", "lib/home_part.dart"}
    assert scopes.visible["lib/other.dart"] == {"lib/other.dart"}


def test_part_to_missing_file_is_ignored() -> None:
    resolver = DartPackageResolver({"demo": ""})
    directives = {"lib/home.dart": [DartDirective("part", "home.g.dart")]}
    scopes = build_scopes(directives, resolver, {"lib/home.dart"})
    assert scopes.visible["lib/home.dart"] == {"lib/home.dart"}
    assert scopes.library_members["lib/home.dart"] == {"lib/home.dart"}


def test_export_cycles_terminate() -> None:
    resolver = DartPackageResolver({})
    directives = {
        "a.dart": [DartDirective("import", "b.dart")],
        "b.dart": [DartDirective("export", "c.dart")],
        "c.dart": [DartDirective("export", "b.dart")],
    }
    scopes = build_scopes(directives, resolver, set(directives))
    assert scopes.visible["a.dart"] == {"a.dart", "b.dart", "c.dart"}


def test_is_private_dart_name() -> None:
    assert is_private_dart_name("_Body")
    assert is_private_dart_name("Foo._bar")
    assert not is_private_dart_name("Foo.bar")
    assert not is_private_dart_name("lib/a.dart::_Body")


def test_part_of_fallback_joins_library_without_part_directive() -> None:
    resolver = DartPackageResolver({})
    directives = {
        "lib/lib.dart": [],
        "lib/extra.dart": [DartDirective("part_of", "lib.dart")],
    }
    scopes = build_scopes(directives, resolver, set(directives))
    assert scopes.library_members["lib/extra.dart"] == {"lib/lib.dart", "lib/extra.dart"}
    assert scopes.visible["lib/lib.dart"] == {"lib/lib.dart", "lib/extra.dart"}
