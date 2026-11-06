"""Tests for the Lean 4 analyzer: header-scanner structure, tree-sitter
references, and exact-name resolution in CallGraphAnalyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import (
    CallGraphAnalyzer,
)
from codewiki.src.be.dependency_analyzer.analyzers.lean import (
    analyze_lean_file,
    mask_source,
    normalize_reference,
    scan_headers,
)
from codewiki.src.be.dependency_analyzer.utils.file_selection import is_skipped_lean_file

POINT = """\
/-! Geometry primitives. -/
namespace Geo

/-- A point in the plane.
Coordinates are naturals. -/
structure Point where
  x : Nat
  y : Nat
  deriving Repr

def Point.add (p q : Point) : Point :=
  let s := p.x + q.x
  { x := s, y := p.y + q.y }

@[simp]
theorem Point.add_zero (p : Point) : Point.add p ⟨0, 0⟩ = p := by
  cases p
  simp [Point.add, Nat.add_zero]

inductive Shape where
  | dot (p : Point)
  | segment (a b : Point)

def Shape.start : Shape → Point
  | .dot p => p
  | .segment a _ => a

private def secret : Nat := 1

instance : Add Point := ⟨Point.add⟩

section Ops
open Point in
def double (p : Point) : Point := add p p
end Ops

end Geo
"""

USE = """\
import Geo.Point

open Geo

def origin : Point := Point.mk 0 0

def first (s : Shape) : Point := Shape.start s

def shapes : List Shape := [Shape.dot origin, .segment origin origin]

def leak : Nat := secret

lemma origin_x : origin.x = 0 := rfl

def report : IO Unit := do
  let p ← pure origin
  IO.println s!"{p.x}"
  #eval first (Shape.dot p)

def after : Point := double origin

def external := Nat.add_comm 1 2
"""

ISLAND = """\
def lonely : Nat := Geo.secret

def notImported := Geo.Point.add
"""


def _write(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


@pytest.fixture
def lean_repo(tmp_path: Path):
    files = [
        _write(tmp_path, "src/Geo/Point.lean", POINT),
        _write(tmp_path, "src/Geo/Use.lean", USE),
        _write(tmp_path, "src/Island.lean", ISLAND),
        _write(tmp_path, ".lake/packages/dep/Dep.lean", "def dep := 1\n"),
    ]
    analyzer = CallGraphAnalyzer()
    analyzer.analyze_code_files([{"path": f, "language": "lean"} for f in files], str(tmp_path))
    edges = {(r.caller, r.callee) for r in analyzer.call_relationships}
    return analyzer.functions, edges


P = "src/Geo/Point.lean::"
U = "src/Geo/Use.lean::"
I = "src/Island.lean::"  # noqa: E741


def test_declarations_namespaces_and_types(lean_repo) -> None:
    functions, _ = lean_repo
    kinds = {f.qualified_name: (f.component_type, f.node_type) for f in functions.values()}
    assert kinds["Geo.Point"] == ("struct", "structure")
    assert kinds["Geo.Point.add"] == ("function", "def")
    assert kinds["Geo.Point.add_zero"] == ("function", "theorem")
    assert kinds["Geo.Shape"] == ("class", "inductive")
    assert kinds["Geo.double"] == ("function", "def")
    assert kinds["origin_x"] == ("function", "theorem")  # `lemma` is a theorem
    assert any(name.startswith("Geo.instance_L") for name in kinds)
    assert all(f.language == "lean" for f in functions.values())


def test_spans_docstrings_and_attributes(lean_repo) -> None:
    functions, _ = lean_repo
    point = functions[P + "Geo.Point"]
    assert point.docstring == "A point in the plane.\nCoordinates are naturals."
    assert (point.start_line, point.end_line) == (6, 9)
    theorem = functions[P + "Geo.Point.add_zero"]
    assert theorem.start_line == 15  # the `@[simp]` line
    assert "simp [Point.add" in (theorem.source_code or "")
    assert not functions[P + "Geo.Point.add"].docstring


def test_lake_folders_are_skipped(lean_repo) -> None:
    functions, _ = lean_repo
    assert not any(".lake" in f.relative_path for f in functions.values())
    assert is_skipped_lean_file(".lake/packages/dep/Dep.lean")
    assert is_skipped_lean_file("lake-packages/std/Std.lean")
    assert not is_skipped_lean_file("src/Geo/Point.lean")


def test_same_file_references_resolve_in_the_namespace(lean_repo) -> None:
    _, edges = lean_repo
    assert (P + "Geo.Point.add", P + "Geo.Point") in edges
    assert (P + "Geo.Point.add_zero", P + "Geo.Point.add") in edges
    assert (P + "Geo.Shape", P + "Geo.Point") in edges  # constructor argument types
    assert (P + "Geo.Shape.start", P + "Geo.Shape") in edges


def test_open_in_applies_to_the_next_declaration(lean_repo) -> None:
    functions, edges = lean_repo
    assert (P + "Geo.double", P + "Geo.Point.add") in edges  # `add` through `open Point in`
    instance = next(i for i in functions if i.startswith(P + "Geo.instance_L"))
    assert (instance, P + "Geo.Point.add") in edges


def test_imports_open_and_member_aliases(lean_repo) -> None:
    _, edges = lean_repo
    assert (U + "origin", P + "Geo.Point") in edges  # `Point.mk` is the structure
    assert (U + "first", P + "Geo.Shape.start") in edges
    assert (U + "shapes", P + "Geo.Shape") in edges  # `Shape.dot` is the inductive
    assert (U + "shapes", U + "origin") in edges
    assert (U + "origin_x", U + "origin") in edges
    assert (U + "after", P + "Geo.double") in edges


def test_error_regions_still_give_references(lean_repo) -> None:
    _, edges = lean_repo
    assert (U + "report", U + "origin") in edges
    assert (U + "report", U + "first") in edges
    assert (U + "report", P + "Geo.Shape") in edges


def test_locals_externals_and_recursion_give_no_edges(lean_repo) -> None:
    functions, edges = lean_repo
    callees = {callee for caller, callee in edges if caller == P + "Geo.Point.add"}
    assert callees == {P + "Geo.Point"}  # p, q, s, p.x are locals
    assert not any(caller == U + "external" for caller, _ in edges)  # Nat.add_comm
    assert all(caller != callee for caller, callee in edges)
    assert all(callee in functions for _, callee in edges)


def test_private_and_unimported_names_are_invisible(lean_repo) -> None:
    _, edges = lean_repo
    assert not any(caller == U + "leak" for caller, _ in edges)  # private in another file
    assert not any(caller.startswith(I) for caller, _ in edges)  # Island imports nothing


def test_ambiguous_name_is_dropped(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "A.lean", "namespace X\ndef f := 1\nend X\n"),
        _write(tmp_path, "B.lean", "namespace Y\ndef f := 2\nend Y\n"),
        _write(tmp_path, "C.lean", "import A\nimport B\nopen X Y\ndef g := f\ndef h := X.f\n"),
    ]
    analyzer = CallGraphAnalyzer()
    analyzer.analyze_code_files([{"path": f, "language": "lean"} for f in files], str(tmp_path))
    edges = {(r.caller, r.callee) for r in analyzer.call_relationships}
    assert not any(caller == "C.lean::g" for caller, _ in edges)
    assert ("C.lean::h", "A.lean::X.f") in edges


def test_root_prefix_mutual_and_nested_namespaces(tmp_path: Path) -> None:
    source = """\
namespace A.B
def _root_.top := 1
mutual
  def even : Nat → Bool
    | 0 => true
    | n + 1 => odd n
  def odd : Nat → Bool
    | 0 => false
    | n + 1 => even n
end
end A.B
def last := top
"""
    nodes, _ = analyze_lean_file(str(tmp_path / "M.lean"), source, repo_path=str(tmp_path))
    names = {n.qualified_name for n in nodes}
    assert names == {"top", "A.B.even", "A.B.odd", "last"}


def test_masking_keeps_lines_and_byte_offsets() -> None:
    source = "def a := \"-- not a comment\" -- é comment\n/- block /- nested -/ -/ def b := 'x'\n"
    masked = mask_source(source)
    assert masked.text.count("\n") == source.count("\n")
    assert len(masked.text.encode("utf8")) == len(source.encode("utf8"))
    assert "not a comment" not in masked.text and "nested" not in masked.text
    assert "def b" in masked.text


def test_interpolated_strings_and_primes_are_masked_correctly() -> None:
    masked = mask_source('def c := s!"x {y} -- z"\ndef d\' := r#"q"# ++ a\'\n')
    assert "-- z" not in masked.text and '"q"' not in masked.text
    assert "def d' :=" in masked.text and "a'" in masked.text


def test_header_scanner_without_the_parse_tree() -> None:
    decls, imports = scan_headers(
        mask_source("import Mathlib.Data.Nat\nimport Foo\n#eval 1\ntheorem t : True := trivial\n")
    )
    assert imports == ["Mathlib.Data.Nat", "Foo"]
    assert [d.fqn for d in decls] == ["t"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Foo.bar", "Foo.bar"),
        ("@Foo.bar", "Foo.bar"),
        ("«weird name».x", "weird name.x"),
        ("p.1", "p"),
        (".dot", None),
        ("fun", None),
    ],
)
def test_normalize_reference(raw: str, expected: str | None) -> None:
    assert normalize_reference(raw) == expected


def test_empty_and_broken_files(tmp_path: Path) -> None:
    assert analyze_lean_file(str(tmp_path / "E.lean"), "", repo_path=str(tmp_path)) == ([], [])
    nodes, _ = analyze_lean_file(
        str(tmp_path / "B.lean"), "def broken (x : := ⟨\n#eval ¬¬\n", repo_path=str(tmp_path)
    )
    assert [n.qualified_name for n in nodes] == ["broken"]
