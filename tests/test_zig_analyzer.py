"""Tests for the tree-sitter based Zig analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analyzers.zig import (
    analyze_zig_file,
    is_skipped_zig_file,
)

SAMPLE = """\
//! Geometry primitives.
const std = @import("std");
const util = @import("util.zig");
const Color = @import("color.zig").Color;
const Shade = util.Shade;

/// A point in the plane.
/// Coordinates are integers.
pub const Point = struct {
    const Self = @This();

    x: i32,
    color: Color,

    pub fn init(x: i32) Point {
        return .{ .x = x, .color = Color.black() };
    }

    pub fn add(self: *Self, other: Point) Point {
        self.validate();
        return util.sum(self.*, other);
    }

    fn validate(self: Self) void {
        _ = self;
    }

    pub const Inner = struct {
        pub fn run() void {}
    };
};

pub const Kind = enum { a, b };
pub const Shape = union(enum) { point: Point, none: void };

pub fn List(comptime T: type) type {
    return struct {
        const Self = @This();
        items: []T,

        pub fn push(self: *Self, item: T) void {
            _ = item;
            self.grow();
        }

        fn grow(self: *Self) void {
            _ = self;
        }
    };
}

pub fn main() !void {
    var p: Point = Point{ .x = 1, .color = Color.black() };
    const q = Point.init(2);
    _ = p.add(q);
    var r = Point.init(3);
    r.validate();
    helper();
    Point.Inner.run();
    util.Shade.dim();
    const s = Shade{};
    _ = s;
    std.debug.print("{d}\\n", .{1});
    var list: List(u8) = undefined;
    list.push(1);
    var unknown = getThing();
    unknown.frobnicate();
}

fn helper() void {}

fn getThing() Thing {
    return undefined;
}

test "helper works" {
    helper();
    Point.Inner.run();
}
"""

UTIL = """\
pub const Shade = struct {
    pub fn dim() void {}
};

pub fn sum(a: anytype, b: anytype) i32 {
    _ = a;
    _ = b;
    return 0;
}

pub extern fn ext_call() void;
"""

COLOR = """\
pub const Color = @import("palette/color.zig").Color;
"""

PALETTE = """\
pub const Color = struct {
    pub fn black() Color {
        return .{};
    }
};
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _analyze(tmp_path: Path, source: str = SAMPLE, name: str = "geo.zig"):
    _write(tmp_path, "util.zig", UTIL)
    _write(tmp_path, "color.zig", COLOR)
    _write(tmp_path, "palette/color.zig", PALETTE)
    file_path = _write(tmp_path, name, source)
    return analyze_zig_file(str(file_path), source, repo_path=str(tmp_path))


def _edges(relationships) -> set[tuple[str, str]]:
    return {(r.caller, r.callee) for r in relationships}


def test_extracts_containers_functions_and_methods(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Point"].component_type == "struct"
    assert by_name["Point"].id == "geo.zig::Point"
    assert by_name["Kind"].component_type == "class"
    assert by_name["Kind"].node_type == "enum"
    assert by_name["Shape"].node_type == "union"
    assert by_name["Point.Inner"].component_type == "struct"

    assert by_name["Point.add"].component_type == "method"
    assert by_name["Point.add"].class_name == "Point"
    assert by_name["Point.Inner.run"].class_name == "Point.Inner"
    assert by_name["main"].component_type == "function"
    assert by_name["main"].class_name is None
    assert all(node.language == "zig" for node in nodes)

    # A function returning `type` is a generic container; its members are
    # keyed by the function name.
    assert by_name["List"].component_type == "struct"
    assert by_name["List"].display_name == "generic struct List"
    assert by_name["List.push"].class_name == "List"

    # Aliases and constants are not components.
    for alias in ("std", "util", "Color", "Shade", "Self"):
        assert alias not in by_name


def test_docstrings_parameters_and_lines(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Point"].docstring == "A point in the plane.\nCoordinates are integers."
    assert by_name["Point"].has_docstring
    assert by_name["helper"].docstring == ""
    assert by_name["Point.add"].parameters == ["self", "other"]
    assert by_name["Point"].start_line == 9
    assert by_name["Point"].end_line == 31
    assert by_name["Point.init"].start_line == 15
    assert by_name["Point.init"].source_code.lstrip().startswith("pub fn init")


def test_same_file_calls_and_methods(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    assert ("geo.zig::main", "geo.zig::helper") in edges
    assert ("geo.zig::main", "geo.zig::Point.init") in edges
    assert ("geo.zig::main", "geo.zig::Point.Inner.run") in edges
    # `q` is typed by Point.init's return type, `p` by its annotation.
    assert ("geo.zig::main", "geo.zig::Point.add") in edges
    # `r` is typed by the `T.init(...)` result.
    assert ("geo.zig::main", "geo.zig::Point.validate") in edges
    # `self: *Self` resolves Self to the enclosing container.
    assert ("geo.zig::Point.add", "geo.zig::Point.validate") in edges
    assert ("geo.zig::List.push", "geo.zig::List.grow") in edges
    # A value of a generic type instance calls its members.
    assert ("geo.zig::main", "geo.zig::List.push") in edges

    resolved = {(r.caller, r.callee): r.is_resolved for r in relationships}
    assert resolved[("geo.zig::main", "geo.zig::helper")] is True


def test_cross_file_aliases_and_reexports(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    assert ("geo.zig::Point.add", "util.zig::sum") in edges
    assert ("geo.zig::main", "util.zig::Shade.dim") in edges
    # `const Shade = util.Shade;` and `Shade{}`.
    assert ("geo.zig::main", "util.zig::Shade") in edges
    # color.zig re-exports palette/color.zig's Color.
    assert ("geo.zig::Point.init", "palette/color.zig::Color.black") in edges
    assert ("geo.zig::Point", "palette/color.zig::Color") in edges

    cross = [r for r in relationships if r.callee.startswith("util.zig::")]
    assert cross and all(not r.is_resolved for r in cross)


def test_type_and_instantiation_edges(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    assert ("geo.zig::main", "geo.zig::Point") in edges  # Point{ ... }
    assert ("geo.zig::Point.add", "geo.zig::Point") in edges  # parameter type
    assert ("geo.zig::Shape", "geo.zig::Point") in edges  # union field
    assert ("geo.zig::main", "geo.zig::List") in edges  # List(u8) annotation
    # No component depends on itself.
    assert all(r.caller != r.callee for r in relationships)


def test_external_calls_and_unknown_values(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    callees = {r.callee for r in relationships}

    assert not any("print" in callee or "std" in callee for callee in callees)
    # A method on a value of unknown type falls back to its bare name.
    unknown = [r for r in relationships if r.callee == "frobnicate"]
    assert unknown and not unknown[0].is_resolved


def test_test_blocks_and_extern_prototypes_are_skipped(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    test_block = SAMPLE.splitlines().index('test "helper works" {') + 1
    assert all(r.call_line < test_block for r in relationships)

    nodes, _ = analyze_zig_file(str(tmp_path / "util.zig"), UTIL, repo_path=str(tmp_path))
    assert {node.name for node in nodes} == {"Shade", "Shade.dim", "sum"}


def test_missing_and_external_imports_drop_calls(tmp_path: Path) -> None:
    source = """\
const gone = @import("gone.zig");
const pkg = @import("some_package");
const outside = @import("../outside.zig");

pub fn run() void {
    gone.call();
    pkg.call();
    outside.call();
}
"""
    _, relationships = _analyze(tmp_path, source, name="run.zig")
    assert relationships == []


def test_build_zig_modules_map_to_root_files(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "build.zig",
        'const httpz = b.addModule("httpz", .{\n    .root_source_file = b.path("src/httpz.zig"),\n});\n',
    )
    _write(tmp_path, "src/httpz.zig", "pub fn serve() void {}\n")
    source = 'const httpz = @import("httpz");\n\npub fn main() void {\n    httpz.serve();\n}\n'
    _, relationships = _analyze(tmp_path, source, name="examples/main.zig")
    assert _edges(relationships) == {("examples/main.zig::main", "src/httpz.zig::serve")}


def test_cache_and_output_dirs_are_skipped(tmp_path: Path) -> None:
    assert is_skipped_zig_file("zig-cache/o/x.zig")
    assert is_skipped_zig_file("pkg/.zig-cache/x.zig")
    assert is_skipped_zig_file("zig-out/lib/x.zig")
    assert not is_skipped_zig_file("src/zig-cache.zig")

    nodes, relationships = _analyze(tmp_path, SAMPLE, name=".zig-cache/o/geo.zig")
    assert nodes == [] and relationships == []


def test_empty_and_broken_files(tmp_path: Path) -> None:
    assert _analyze(tmp_path, "", name="empty.zig") == ([], [])
    nodes, _ = _analyze(tmp_path, "pub fn broken( {\n", name="broken.zig")
    assert isinstance(nodes, list)
