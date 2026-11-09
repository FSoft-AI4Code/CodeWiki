"""Tests for the Lean 4 analyzer: header-scanner structure, tree-sitter
references, and exact-name resolution in CallGraphAnalyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer import (  # noqa: E402
    CallGraphAnalyzer,
)
from codewiki.src.be.dependency_analyzer.analyzers.lean import (  # noqa: E402
    analyze_lean_file,
    mask_source,
    normalize_reference,
    scan_headers,
)
from codewiki.src.be.dependency_analyzer.utils.file_selection import is_skipped_lean_file  # noqa: E402

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


def _analyze_sources(tmp_path: Path, sources: dict[str, str], *, reverse: bool = False):
    files = [_write(tmp_path, rel, source) for rel, source in sources.items()]
    analyzer = CallGraphAnalyzer()
    analyzer.analyze_code_files(
        [{"path": rel, "language": "lean"} for rel in (reversed(files) if reverse else files)],
        str(tmp_path),
    )
    return analyzer, {(r.caller, r.callee) for r in analyzer.call_relationships}


@pytest.mark.parametrize(
    "binder",
    [
        "fun (x : Foo) => x",
        "λ (x : Foo) => x",
        "fun x : Foo => x",
        "fun (x y : Foo) => x",
        "fun {x : Foo} => x",
        "fun ⦃x : Foo⦄ => x",
        "fun [inst : Foo] => inst",
        "fun [Foo] => 1",
        "∀ (x : Foo), x = x",
        "∃ (x : Foo), x = x",
        "∃! (x : Foo), x = x",
        "Σ (x : Foo), Foo",
        "Π (x : Foo), Foo",
    ],
)
def test_typed_binders_preserve_type_dependencies(tmp_path: Path, binder: str) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": f"structure Foo where\n  value : Nat\ndef use := {binder}\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::Foo")}


def test_nested_binder_types_are_not_local_names(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "structure Foo where\n  value : Nat\n"
            "structure Bar where\n  value : Nat\n"
            "def use := fun (x : Foo × (List Bar)) (y : Bar := ⟨1⟩) => x\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::Foo"), ("M.lean::use", "M.lean::Bar")}


def test_untyped_binders_suppress_globals(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "def x := 1\ndef y := 2\ndef use := fun x y => x + y\n",
        },
    )
    assert not edges


@pytest.mark.parametrize(
    "header, ending",
    [
        ("namespace A\nnamespace B", "end A.B"),
        ("namespace A.B", "end A.B"),
        ("section A\nsection B", "end A.B"),
        ("section A.B", "end A.B"),
    ],
)
def test_named_end_closes_matching_suffix(tmp_path: Path, header: str, ending: str) -> None:
    analyzer, _ = _analyze_sources(
        tmp_path,
        {
            "M.lean": f"{header}\ndef inside := 1\n{ending}\ndef outside := 2\n",
        },
    )
    assert "M.lean::outside" in analyzer.functions


def test_partial_namespace_end_and_section_barrier(tmp_path: Path, caplog) -> None:
    analyzer, _ = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A.B\nnamespace C\nend B.C\ndef first := 1\n"
            "section\nnamespace D.E\nend A.D.E\ndef retained := 2\n"
            "end D.E\nend\nend A\ndef last := 3\n",
        },
    )
    assert set(analyzer.functions) == {"M.lean::A.first", "M.lean::A.D.E.retained", "M.lean::last"}
    assert "Mismatched Lean end" in caplog.text


def test_section_variables_are_scoped_and_restored(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "def x := 1\ndef y := 2\nvariable (x : Nat)\n"
            "section Outer.Inner\nvariable {y : Nat}\ndef inside := x + y\n"
            "end Inner\ndef middle := x + y\nend Outer\ndef outside := x + y\n",
        },
    )
    assert edges == {("M.lean::middle", "M.lean::y"), ("M.lean::outside", "M.lean::y")}


def test_namespace_variables_expire_and_types_are_not_binders(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "structure Foo where\n  value : Nat\ndef x := 1\n"
            "namespace A\nvariable\n  (x : Foo)\n  [Inhabited Foo]\n"
            "def inside := (x, (⟨1⟩ : Foo))\nend A\ndef outside := x\n",
        },
    )
    assert edges == {("M.lean::A.inside", "M.lean::Foo"), ("M.lean::outside", "M.lean::x")}


_OPEN_FIXTURE = "namespace A\ndef f := 1\ndef g := 2\nend A\nnamespace B\ndef g := 3\nend B\n"


@pytest.mark.parametrize("opening", ["open A (f)", "open A hiding g", "open scoped A"])
def test_open_restrictions_do_not_create_ambiguity(tmp_path: Path, opening: str) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + f"{opening}\nopen B\ndef use := g\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::B.g")}


@pytest.mark.parametrize(
    "opening, expected",
    [
        ("open A (f)", {"A.f"}),
        ("open A hiding g", {"A.f"}),
        ("open scoped A", set()),
        ("open A renaming f → h", {"A.f"}),
        ("open A renaming f -> h, g -> k", {"A.f", "A.g"}),
        ("open A\n  renaming\n    f → h,\n    g → k", {"A.f", "A.g"}),
        ("open A\n  hiding g", {"A.f"}),
        ("open A\n  (f)", {"A.f"}),
    ],
)
def test_open_modes_only_expose_the_selected_names(
    tmp_path: Path, opening: str, expected: set
) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + f"{opening}\ndef use := (f, g, h, k)\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::" + name) for name in expected}


@pytest.mark.parametrize(
    "opening, exposed",
    [
        ("open A", False),
        ("open A hiding other", False),
        ("open A (secret)", True),
        ("open A renaming secret → value", True),
    ],
)
def test_protected_names_require_explicit_open(tmp_path: Path, opening: str, exposed: bool) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A\nprotected def secret := 1\ndef other := 2\nend A\n"
            + opening
            + "\ndef use := (secret, value)\n",
        },
    )
    assert edges == ({("M.lean::use", "M.lean::A.secret")} if exposed else set())


@pytest.mark.parametrize("same_line", [False, True])
def test_restricted_open_in_expires(tmp_path: Path, same_line: bool) -> None:
    separator = " " if same_line else "\n"
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE
            + "open A renaming f → value in"
            + separator
            + "def inside := value\ndef outside := value\n",
        },
    )
    assert edges == {("M.lean::inside", "M.lean::A.f")}


def test_open_in_applies_to_a_namespace_block(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "open A in\nnamespace Inner\n"
            "def inside := f\nend Inner\ndef outside := f\n",
        },
    )
    assert edges == {("M.lean::Inner.inside", "M.lean::A.f")}


def test_open_uses_the_command_namespace(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A\ndef f := 1\nend A\n"
            "namespace B.A\ndef f := 2\nend B.A\nopen A\n"
            "namespace B\ndef use := f\nend B\n",
        },
    )
    assert edges == {("M.lean::B.use", "M.lean::A.f")}


def test_relative_and_sequential_opens(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A.B.C\ndef f := 1\nend A.B.C\n"
            "namespace A\nopen B C\ndef inside := f\nend A\n"
            "def outside := f\n",
        },
    )
    assert edges == {("M.lean::A.inside", "M.lean::A.B.C.f")}


def test_later_namespace_does_not_rebind_an_open(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A\ndef f := 1\nend A\nnamespace B\nopen A\n"
            "namespace A\ndef f := 2\nend A\ndef use := f\nend B\n",
        },
    )
    assert edges == {("M.lean::B.use", "M.lean::A.f")}


@pytest.mark.parametrize("reverse", [False, True])
def test_exports_are_persistent_visible_aliases(tmp_path: Path, reverse: bool) -> None:
    analyzer, edges = _analyze_sources(
        tmp_path,
        {
            "Base.lean": "namespace A\ndef f := 1\ndef g := 2\nend A\n",
            "Exports.lean": "import Base\nnamespace B\nsection\nexport A (f)\nend\nend B\n",
            "Reexport.lean": "import Exports\nnamespace C\nexport B (f)\n"
            "def inside := f\nend C\ndef outside := C.f\n",
            "Use.lean": "import Reexport\nopen C\ndef use := (f, B.f, C.f)\n",
            "Island.lean": "import Base\ndef missing := (B.f, C.f)\n",
        },
        reverse=reverse,
    )
    assert edges == {
        ("Reexport.lean::C.inside", "Base.lean::A.f"),
        ("Reexport.lean::outside", "Base.lean::A.f"),
        ("Use.lean::use", "Base.lean::A.f"),
    }
    assert len(analyzer.functions) == 6  # aliases introduce no components


def test_exports_only_expose_selected_names_and_keep_command_context(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "namespace Outer\nopen A\n"
            "namespace Inner\nexport _root_.A (f)\nend Inner\nend Outer\n"
            "open Outer.Inner\ndef use := (f, g, Outer.Inner.f)\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::A.f")}


def test_root_and_constructor_exports_resolve_to_original_components(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A\ninductive Choice where\n  | first\n  | second\n"
            "export Choice (first)\nend A\nexport A (first)\ndef use := first\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::A.Choice")}


def test_export_is_not_visible_before_its_command(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "def before := f\nexport A (f)\ndef after := f\n",
        },
    )
    assert edges == {("M.lean::after", "M.lean::A.f")}


def test_private_and_ambiguous_exports_are_not_linked(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "Base.lean": "namespace A\nprivate def secret := 1\ndef f := 2\nend A\n",
            "Other.lean": "namespace A\ndef f := 3\nend A\n",
            "Exports.lean": "import Base\nimport Other\nexport A (secret f)\ndef use := (secret, f)\n",
            "Use.lean": "import Exports\ndef use := (secret, f)\n",
        },
    )
    assert not edges


def test_lean_resolution_state_is_reset_between_runs(tmp_path: Path) -> None:
    analyzer, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "export A (f)\ndef use := f\n",
        },
    )
    assert edges
    _write(tmp_path, "M.lean", "def use := f\n")
    analyzer.analyze_code_files([{"path": "M.lean", "language": "lean"}], str(tmp_path))
    assert not analyzer.call_relationships


def test_malformed_directives_do_not_abort_the_file(tmp_path: Path) -> None:
    analyzer, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "open A renaming f →\nexport A (\nend Nope\n"
            "def after := A.f\n",
        },
    )
    assert "M.lean::after" in analyzer.functions
    assert edges == {("M.lean::after", "M.lean::A.f")}


@pytest.mark.parametrize("separator", [" ", "\n"])
def test_nested_open_in_wrappers_expire(tmp_path: Path, separator: str) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A.B\ndef f := 1\nend A.B\n"
            + "open A in"
            + separator
            + "open B in"
            + separator
            + "@[simp] def inside := f\ndef outside := f\n",
        },
    )
    assert edges == {("M.lean::inside", "M.lean::A.B.f")}


def test_open_in_does_not_leak_an_inner_open_command(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A.B\ndef f := 1\nend A.B\nopen A in open B\ndef outside := f\n",
        },
    )
    assert not edges


def test_open_in_is_consumed_by_a_non_declaration(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "open A in\n#check f\ndef outside := f\n",
        },
    )
    assert not edges


def test_named_end_restores_opens_and_variables(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": _OPEN_FIXTURE + "def x := 1\nsection Outer.Inner\n"
            "open A\nvariable (x : Nat)\ndef inside := (f, x)\n"
            "end Outer.Inner\ndef outside := (f, x)\n",
        },
    )
    assert edges == {("M.lean::inside", "M.lean::A.f"), ("M.lean::outside", "M.lean::x")}


def test_selective_open_disambiguates_each_selected_name(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A.B\ndef f := 1\nend A.B\nnamespace B\ndef g := 2\nend B\n"
            "open A\nopen B (f g)\ndef use := (f, g)\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::A.B.f"), ("M.lean::use", "M.lean::B.g")}


def test_duplicate_exports_to_the_same_target_are_not_ambiguous(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "Base.lean": _OPEN_FIXTURE,
            "First.lean": "import Base\nnamespace Shared\nexport A (f)\nend Shared\n",
            "Second.lean": "import Base\nnamespace Shared\nexport A (f)\nend Shared\n",
            "Use.lean": "import First\nimport Second\ndef use := Shared.f\n",
        },
    )
    assert edges == {("Use.lean::use", "Base.lean::A.f")}


def test_export_source_resolves_through_an_open_namespace(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A.B\ndef f := 1\nend A.B\n"
            "namespace C\nopen A\nexport B (f)\nend C\ndef use := C.f\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::A.B.f")}


def test_export_keeps_the_namespace_at_its_command(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "namespace A\ndef f := 1\nend A\nnamespace C\nexport A (f)\n"
            "namespace A\ndef f := 2\nend A\ndef use := f\nend C\n",
        },
    )
    assert edges == {("M.lean::C.use", "M.lean::A.f")}


def test_invalid_import_cycle_and_unresolved_exports_do_not_crash(tmp_path: Path) -> None:
    analyzer, edges = _analyze_sources(
        tmp_path,
        {
            "A.lean": "import B\nexport Missing (f)\ndef a := 1\n",
            "B.lean": "import A\nopen Missing\ndef b := 2\n",
        },
    )
    assert set(analyzer.functions) == {"A.lean::a", "B.lean::b"}
    assert not edges


@pytest.mark.parametrize(
    "body",
    [
        "fun (x : (Foo : Type)) => x",
        "fun [Inhabited (Foo : Type)] => (⟨1⟩ : Foo)",
        "((⟨1⟩ : Foo), (Foo : Type))",
    ],
)
def test_type_ascriptions_are_not_mistaken_for_binders(tmp_path: Path, body: str) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "structure Foo where\n  value : Nat\ndef use := " + body + "\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::Foo")}


def test_declaration_binders_still_work_in_error_regions(tmp_path: Path) -> None:
    _, edges = _analyze_sources(
        tmp_path,
        {
            "M.lean": "structure Foo where\n  value : Nat\ndef x := 1\n"
            "def use\n  (x : Foo)\n  : Foo := by\n  exact x\n",
        },
    )
    assert edges == {("M.lean::use", "M.lean::Foo")}
