"""Tests for the tree-sitter based Go analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analyzers.artifact import (
    ArtifactOptions,
    classify_artifact,
)
from codewiki.src.be.dependency_analyzer.analyzers.go import (
    analyze_go_file,
    go_package_ref,
    split_go_package_ref,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

SAMPLE = """\
package buffer

import (
	"fmt"
	"strings"
)

// Flusher is implemented by buffers that can be flushed.
type Flusher interface {
	Flush() error
}

// Logger writes structured messages.
type Logger struct {
	prefix string
}

func NewLogger() *Logger {
	return &Logger{prefix: "buf"}
}

// Warn logs a warning.
func (l *Logger) Warn(message string) {
	fmt.Println(l.prefix, message)
}

type Event struct {
	ID uint64
}

// Kind tells events apart.
type Kind int

func (k Kind) Label() string { return "kind" }

type (
	// Signal carries an event downstream.
	Signal struct {
		event *Event
		kind  Kind
	}
	Handler func(ev Event) error
)

// Buffer holds events before flushing them.
type Buffer[T any] struct {
	Logger
	capacity int
	items    []T
	index    map[uint64]*Event
	sink     Flusher
}

func NewBuffer[T any](capacity int) *Buffer[T] {
	return &Buffer[T]{capacity: capacity, Logger: *NewLogger()}
}

func (b *Buffer[T]) Push(item T) {
	b.validate()
	b.Warn("pushed")
	local := NewLogger()
	local.Warn("local")
	var event Event
	record(&event)
	b.items = append(b.items, item)
	var sb strings.Builder
	sb.WriteString("x")
	k := Kind(1)
	k.Label()
	b.sink.Flush()
	callback := func() { helper() }
	callback()
}

func (b Buffer[T]) validate() bool {
	return len(b.items) < b.capacity
}

func (b *Buffer[T]) Flush() error {
	b.items = nil
	return nil
}

func record(ev *Event) {}
"""


def _analyze(tmp_path: Path, source: str = SAMPLE, name: str = "buffer.go"):
    file_path = tmp_path / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(source, encoding="utf-8")
    return analyze_go_file(str(file_path), source, repo_path=str(tmp_path))


def _edges(relationships) -> set[tuple[str, str]]:
    return {(r.caller, r.callee) for r in relationships}


def test_extracts_types_functions_and_methods(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Buffer"].component_type == "struct"
    assert by_name["Flusher"].component_type == "interface"
    assert by_name["Kind"].component_type == "class"
    assert by_name["Kind"].node_type == "type"
    assert by_name["Handler"].node_type == "type"
    assert by_name["Signal"].component_type == "struct"

    # Pointer and value receivers both key the method by the base type.
    assert by_name["Buffer.Push"].component_type == "method"
    assert by_name["Buffer.Push"].class_name == "Buffer"
    assert by_name["Buffer.validate"].class_name == "Buffer"
    assert by_name["Kind.Label"].class_name == "Kind"
    # Interface method signatures are owned by the interface.
    assert by_name["Flusher.Flush"].component_type == "method"

    assert by_name["NewBuffer"].component_type == "function"
    assert by_name["Buffer"].id == "buffer.go::Buffer"
    assert by_name["Buffer.Push"].id == "buffer.go::Buffer.Push"
    assert all(node.language == "go" for node in nodes)


def test_docstrings_and_parameters(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Buffer"].docstring == "// Buffer holds events before flushing them."
    # A spec inside a grouped `type (...)` takes the comment above the spec.
    assert by_name["Signal"].docstring == "// Signal carries an event downstream."
    assert not by_name["Event"].has_docstring
    assert by_name["Logger.Warn"].parameters == ["message string"]


def test_embedding_and_interfaces_become_base_classes(tmp_path: Path) -> None:
    nodes, relationships = _analyze(tmp_path)
    buffer = next(n for n in nodes if n.name == "Buffer")
    assert buffer.base_classes == ["Logger"]
    assert ("buffer.go::Buffer", "buffer.go::Logger") in _edges(relationships)


def test_type_edges(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    # Field types through pointers, maps, and named types.
    assert ("buffer.go::Buffer", "buffer.go::Event") in edges
    assert ("buffer.go::Buffer", "buffer.go::Flusher") in edges
    assert ("buffer.go::Signal", "buffer.go::Event") in edges
    assert ("buffer.go::Signal", "buffer.go::Kind") in edges
    # The underlying type of a defined type.
    assert ("buffer.go::Handler", "buffer.go::Event") in edges
    # Type parameters and predeclared types never become edges.
    callees = {callee for _, callee in edges}
    assert not any(c.endswith(("::T", "::int", "::uint64", "::any")) for c in callees)


def test_call_and_literal_edges(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    # Composite literals, through `&` and type arguments.
    assert ("buffer.go::NewLogger", "buffer.go::Logger") in edges
    assert ("buffer.go::NewBuffer", "buffer.go::Buffer") in edges
    assert ("buffer.go::NewBuffer", "buffer.go::NewLogger") in edges
    # Receiver method calls (value and pointer receivers).
    assert ("buffer.go::Buffer.Push", "buffer.go::Buffer.validate") in edges
    # A local whose type comes from a same-file constructor's result.
    assert ("buffer.go::Buffer.Push", "buffer.go::Logger.Warn") in edges
    # A conversion `Kind(1)` types the local.
    assert ("buffer.go::Buffer.Push", "buffer.go::Kind.Label") in edges
    # A field's interface type resolves the method on the interface.
    assert ("buffer.go::Buffer.Push", "buffer.go::Flusher.Flush") in edges
    assert ("buffer.go::Buffer.Push", "buffer.go::record") in edges
    # Types named in signatures and local `var`s (not the receiver).
    assert ("buffer.go::record", "buffer.go::Event") in edges
    assert ("buffer.go::NewLogger", "buffer.go::Logger") in edges
    assert ("buffer.go::Buffer.Push", "buffer.go::Event") in edges
    assert ("buffer.go::Buffer.validate", "buffer.go::Buffer") not in edges


def test_promoted_method_is_left_to_package_resolution(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    # `b.Warn()` is promoted from the embedded Logger: the file analyzer
    # names `Buffer.Warn` in the package, CallGraphAnalyzer follows the
    # embedding.
    assert ("buffer.go::Buffer.Push", go_package_ref("", "Buffer.Warn")) in _edges(relationships)


def test_external_and_builtin_calls_are_dropped(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    callees = {r.callee for r in relationships}

    for noise in ("Println", "WriteString", "append", "len", "fmt", "strings"):
        assert noise not in callees
    assert not any("Println" in c or "WriteString" in c for c in callees)
    # A closure held in a local is not a call to a package function, but
    # the closure body's call is attributed to the enclosing function.
    assert go_package_ref("", "callback") not in callees
    assert go_package_ref("", "helper") in callees


def test_package_ref_round_trip() -> None:
    assert go_package_ref("", "Get") == "*.go::Get"
    assert go_package_ref("sub/pkg", "T.M") == "sub/pkg/*.go::T.M"
    assert split_go_package_ref("sub/pkg/*.go::T.M") == ("sub/pkg", "T.M")
    assert split_go_package_ref("*.go::Get") == ("", "Get")
    assert split_go_package_ref("buffer.go::Get") is None
    assert split_go_package_ref("Get") is None


def test_test_files_and_vendor_are_skipped(tmp_path: Path) -> None:
    source = "package buffer\n\nfunc TestPush(t *testing.T) { record(nil) }\n"
    assert _analyze(tmp_path, source, "buffer_test.go") == ([], [])
    vendored = "package dep\n\nfunc Helper() {}\n"
    assert _analyze(tmp_path, vendored, "vendor/github.com/x/dep/dep.go") == ([], [])


def test_empty_and_broken_files_return_no_components(tmp_path: Path) -> None:
    assert _analyze(tmp_path, "", "empty.go") == ([], [])
    nodes, _ = _analyze(tmp_path, "package x\nfunc broken( {{{ type", "broken.go")
    assert isinstance(nodes, list)


def test_go_mod_classifies_as_manifest() -> None:
    opts = ArtifactOptions()
    assert classify_artifact("go.mod", "go.mod", 100, opts) == "manifest"
    assert classify_artifact("main.go", "main.go", 100, opts) is None


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dependency_parser_end_to_end(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module github.com/acme/shop\n\ngo 1.22\n")
    _write(
        tmp_path,
        "store/store.go",
        "package store\n\n"
        "type Config struct { Name string }\n\n"
        "type Store struct { cfg Config }\n\n"
        "func Open(cfg Config) *Store { return &Store{cfg: cfg} }\n\n"
        "func (s *Store) Save(key string) error { return nil }\n\n"
        "func (s *Store) Load(key string) string { return key }\n",
    )
    # Two files of one package: names cross files without imports.
    _write(
        tmp_path,
        "cart/cart.go",
        "package cart\n\n"
        "import (\n"
        '\t"fmt"\n\n'
        '\tdb "github.com/acme/shop/store"\n'
        ")\n\n"
        "type Base struct{}\n\n"
        "func (Base) Describe() string { return describe() }\n\n"
        "type Cart struct {\n"
        "\tBase\n"
        "\tstore *db.Store\n"
        "\tlines []Line\n"
        "}\n\n"
        "type Saver interface { Save(key string) error }\n\n"
        "func New() *Cart {\n"
        '\ts := db.Open(db.Config{Name: "carts"})\n'
        "\treturn &Cart{store: s}\n"
        "}\n\n"
        "func (c *Cart) Save(key string) error {\n"
        "\tc.store.Save(key)\n"
        "\tfmt.Println(c.Describe())\n"
        "\treturn c.store.Load(key)\n"
        "}\n",
    )
    _write(
        tmp_path,
        "cart/line.go",
        "package cart\n\ntype Line struct{ Qty int }\n\nfunc describe() string { return total() }\n",
    )
    _write(tmp_path, "cart/total.go", "package cart\n\nfunc total() string { return \"\" }\n")
    _write(
        tmp_path,
        "cart/cart_test.go",
        "package cart\n\nimport \"testing\"\n\nfunc TestNew(t *testing.T) { New() }\n",
    )

    components = DependencyParser(str(tmp_path)).parse_repository()

    assert "cart/cart.go::Cart" in components
    assert "cart/cart.go::Cart.Save" in components
    assert "store/store.go::Store.Save" in components
    # _test.go files contribute nothing.
    assert not any(cid.startswith("cart/cart_test.go") for cid in components)

    cart = components["cart/cart.go::Cart"].depends_on
    # Cross-file field type in the same package, and a type of another
    # package imported under an alias through go.mod's module path.
    assert "cart/line.go::Line" in cart
    assert "store/store.go::Store" in cart
    # Embedded struct, and the interface of its package that Cart implements.
    assert "cart/cart.go::Base" in cart
    assert "cart/cart.go::Saver" in cart
    assert "Saver" in components["cart/cart.go::Cart"].base_classes

    new = components["cart/cart.go::New"].depends_on
    assert "store/store.go::Open" in new
    assert "store/store.go::Config" in new
    assert "cart/cart.go::Cart" in new

    save = components["cart/cart.go::Cart.Save"].depends_on
    # Method calls through a field of another package's type.
    assert "store/store.go::Store.Save" in save
    assert "store/store.go::Store.Load" in save
    # A method promoted from the embedded Base.
    assert "cart/cart.go::Base.Describe" in save
    # fmt is external: nothing resolves to it.
    assert all(dep in components for dep in save)

    # Same-package call to a function in another file.
    assert "cart/line.go::describe" in components["cart/cart.go::Base.Describe"].depends_on
    assert "cart/total.go::total" in components["cart/line.go::describe"].depends_on
    # Store does not have Save via Cart's Saver: interfaces only link
    # within their own package.
    assert "cart/cart.go::Saver" not in components["store/store.go::Store"].depends_on


def test_unknown_receiver_resolves_only_when_unique(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/m\n")
    _write(
        tmp_path,
        "a.go",
        "package m\n\n"
        "type Parser struct{}\n\n"
        "func (p *Parser) ParseAll() {}\n\n"
        "type Lexer struct{}\n\n"
        "func (l *Lexer) Step() {}\n\n"
        "type Walker struct{}\n\n"
        "func (w *Walker) Step() {}\n\n"
        "func Run(next func() *Parser, x interface{ Step() }) {\n"
        "\tnext().ParseAll()\n"
        "\tx.Step()\n"
        "}\n",
    )

    components = DependencyParser(str(tmp_path)).parse_repository()
    run = components["a.go::Run"].depends_on
    assert "a.go::Parser.ParseAll" in run
    # Two `Step` methods in scope: ambiguous, no guess.
    assert not any(dep.endswith(".Step") for dep in run)


def test_local_types_follow_scope_results_and_elements(tmp_path: Path) -> None:
    source = """\
package m

type Result struct{ Raw string }

func (r Result) Get(key string) Result { return r }

func (r Result) Exists() bool { return true }

func parse(s string) (int, Result, bool) { return 0, Result{}, true }

func Walk(all []Result, byKey map[string]*Result) {
\t_, res, ok := parse("x")
\tif ok {
\t\tres := res.Get("a")
\t\tres.Exists()
\t}
\tfor _, r := range all {
\t\tr.Exists()
\t}
\tbyKey["k"].Get("b")
}
"""
    _, relationships = _analyze(tmp_path, source, "walk.go")
    edges = _edges(relationships)
    assert ("walk.go::Walk", "walk.go::Result.Get") in edges
    assert ("walk.go::Walk", "walk.go::Result.Exists") in edges
    assert all(r.is_resolved for r in relationships)


def test_local_replace_directive_maps_another_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "go.mod",
        "module example.com/app\n\n"
        "require example.com/lib v0.0.0\n\n"
        "replace (\n\texample.com/lib v0.0.0 => ./lib // local copy\n)\n",
    )
    _write(tmp_path, "lib/go.mod", "module example.com/lib\n")
    _write(tmp_path, "lib/util/util.go", "package util\n\nfunc Trim(s string) string { return s }\n")
    _write(
        tmp_path,
        "main.go",
        'package main\n\nimport "example.com/lib/util"\n\nfunc main() { util.Trim("x") }\n',
    )

    components = DependencyParser(str(tmp_path)).parse_repository()
    assert "lib/util/util.go::Trim" in components["main.go::main"].depends_on
