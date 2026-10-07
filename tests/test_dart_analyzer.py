"""Tests for the tree-sitter based Dart analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.cli.utils.validation import detect_supported_languages
from codewiki.src.be.dependency_analyzer.analyzers.dart import (
    TreeSitterDartAnalyzer,
    analyze_dart_file,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

SAMPLE = """\
import 'package:demo/models/car.dart' as car;

/// Logs messages.
mixin Loggable on Object {
  void log(String m) => print(m);
}

abstract class Repo<T> {
  Future<T> fetch();
}

abstract interface class Disposable {
  void dispose();
}

sealed class Shape {}

/// Cars repo.
class CarRepo extends Repo<Car> with Loggable implements Disposable {
  final ApiClient api;
  CarRepo(this.api);
  factory CarRepo.create() => CarRepo(ApiClient());

  /// Loads the car.
  @override
  Future<Car> fetch() async {
    final r = await api.get('/car');
    log('x');
    return Car.fromJson(r);
  }

  int get count => 1;
  set count(int v) {}

  @override
  void dispose() {}
}

extension StringX on String {
  String shout() => toUpperCase();
}

extension on int {
  int twice() => this * 2;
}

enum Status {
  idle,
  busy;

  bool get isIdle => this == Status.idle;
}

int helper(int a, {int b = 2}) => a + b;

String get appName => 'demo';
"""


def _analyze(tmp_path: Path, source: str = SAMPLE, name: str = "lib/repo.dart"):
    file_path = tmp_path / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(source, encoding="utf-8")
    return analyze_dart_file(str(file_path), source, repo_path=str(tmp_path))


def _by_name(nodes):
    return {node.name: node for node in nodes}


def test_extracts_declarations_with_types(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = _by_name(nodes)
    expected = {
        "Loggable": ("class", "mixin"),
        "Loggable.log": ("method", "method"),
        "Repo": ("class", "abstract class"),
        "Repo.fetch": ("method", "method"),
        "Disposable": ("interface", "interface"),
        "Disposable.dispose": ("method", "method"),
        "Shape": ("class", "sealed class"),
        "CarRepo": ("class", "class"),
        "CarRepo.fetch": ("method", "method"),
        "CarRepo.count": ("method", "getter"),
        "CarRepo.dispose": ("method", "method"),
        "StringX": ("class", "extension"),
        "StringX.shout": ("method", "method"),
        "extension_on_int": ("class", "extension"),
        "extension_on_int.twice": ("method", "method"),
        "Status": ("class", "enum"),
        "Status.isIdle": ("method", "getter"),
        "helper": ("function", "function"),
        "appName": ("function", "getter"),
    }
    assert {name: (n.component_type, n.node_type) for name, n in by_name.items()} == expected
    # Constructors and setters are not components.
    assert "CarRepo.create" not in by_name
    assert all(node.language == "dart" for node in nodes)
    assert by_name["CarRepo"].id == "lib/repo.dart::CarRepo"
    assert by_name["CarRepo.fetch"].class_name == "CarRepo"


def test_bases_docstrings_parameters_and_spans(tmp_path: Path) -> None:
    by_name = _by_name(_analyze(tmp_path)[0])
    assert by_name["CarRepo"].base_classes == ["Repo", "Loggable", "Disposable"]
    assert by_name["Loggable"].base_classes == ["Object"]
    assert by_name["StringX"].base_classes == ["String"]
    assert by_name["CarRepo"].docstring == "/// Cars repo."
    # Doc comment is found across the @override annotation.
    assert by_name["CarRepo.fetch"].docstring == "/// Loads the car."
    assert by_name["helper"].parameters == ["a", "b"]
    fetch = by_name["CarRepo.fetch"]
    assert fetch.source_code.lstrip().startswith("Future<Car> fetch()")
    assert fetch.source_code.rstrip().endswith("}")
    assert fetch.end_line - fetch.start_line == 4


def test_import_prefixes_and_directives(tmp_path: Path) -> None:
    file_path = tmp_path / "lib" / "repo.dart"
    file_path.parent.mkdir(parents=True)
    file_path.write_text(SAMPLE, encoding="utf-8")
    analyzer = TreeSitterDartAnalyzer(str(file_path), SAMPLE, repo_path=str(tmp_path))
    assert analyzer.import_prefixes == {"car"}
    assert analyzer.relative_path == "lib/repo.dart"
    assert [d.kind for d in analyzer.directives] == ["import"]


def test_parse_errors_keep_surrounding_declarations(tmp_path: Path) -> None:
    source = (
        "library;\n"
        "class Before {}\n"
        "extension type CarId(int v) {}\n"
        "Map<String, Object?> body(String? pin) => {'pin': ?pin};\n"
        "class After {\n  void run() {}\n}\n"
    )
    by_name = _by_name(_analyze(tmp_path, source, "lib/broken.dart")[0])
    assert {"Before", "After", "After.run"} <= set(by_name)


def test_empty_and_garbage_files_return_nothing(tmp_path: Path) -> None:
    assert _analyze(tmp_path, "", "lib/empty.dart") == ([], [])
    nodes, _ = _analyze(tmp_path, "class {{{ void (", "lib/garbage.dart")
    assert isinstance(nodes, list)


def test_generated_dart_files_are_ignored(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    (lib / "car.dart").write_text("part 'car.g.dart';\nclass Car {}\n", encoding="utf-8")
    (lib / "car.g.dart").write_text("part of 'car.dart';\nclass _$CarGen {}\n", encoding="utf-8")
    (lib / "car.freezed.dart").write_text("class _$CarFreezed {}\n", encoding="utf-8")
    tool = tmp_path / ".dart_tool"
    tool.mkdir()
    (tool / "gen.dart").write_text("class Hidden {}\n", encoding="utf-8")

    components = DependencyParser(str(tmp_path)).parse_repository()

    dart_ids = {cid for cid, node in components.items() if node.language == "dart"}
    assert dart_ids == {"lib/car.dart::Car"}


def test_dart_is_a_detected_language(tmp_path: Path) -> None:
    (tmp_path / "main.dart").write_text("void main() {}\n", encoding="utf-8")
    assert ("Dart", 1) in detect_supported_languages(tmp_path)


def _edges(relationships):
    return {
        (r.caller.split("::")[-1], r.callee.split("::")[-1], r.is_resolved) for r in relationships
    }


def test_inheritance_and_field_type_edges(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    assert ("CarRepo", "Repo", True) in edges
    assert ("CarRepo", "Loggable", True) in edges
    assert ("CarRepo", "Disposable", True) in edges
    assert ("CarRepo", "Car", False) in edges  # type argument of the superclass
    assert ("CarRepo", "ApiClient", False) in edges  # field type
    # Core types and type parameters never become edges.
    callees = {callee for _, callee, _ in edges}
    assert not callees & {"Future", "String", "Object", "int", "T"}


def test_call_and_instantiation_edges(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    assert ("CarRepo.fetch", "ApiClient.get", False) in edges  # typed field receiver
    assert ("CarRepo.fetch", "log", False) in edges  # inherited, resolved cross-file later
    assert ("CarRepo.fetch", "Car", False) in edges  # Car.fromJson -> type edge
    assert ("CarRepo", "CarRepo", True) not in edges  # no self edges
    assert ("CarRepo", "ApiClient", False) in edges  # factory body -> class-level
    assert ("Status.isIdle", "Status", True) in edges  # enum value access
    callees = {callee for _, callee, _ in edges}
    assert "print" not in callees
    assert "toUpperCase" not in callees


CALLS = """\
import 'package:demo/models.dart' as m;

class Engine {
  void start() {}
  void restart() {
    stop();
    this.start();
    start();
  }
  void stop() {}
}

Engine build() => Engine();

class Garage {
  final Engine engine;
  Garage(this.engine);

  void open(Engine spare, {required Door door}) {
    engine.start();
    spare.restart();
    door.unlock();
    final Window w = Window();
    w.close();
    final inferred = Engine();
    inferred.stop();
    engine..start()..stop();
    final box = Box<Item>(1);
    final made = new Crate<int>();
    const label = Label('x');
    final car = m.Car(1);
    final named = Window.tinted();
    build();
    unknown.call();
  }
}
"""


def test_call_resolution_rules(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path, CALLS, "lib/garage.dart")[1])
    expected_present = {
        ("Engine.restart", "Engine.stop", True),
        ("Engine.restart", "Engine.start", True),  # this.start() and bare start()
        ("Garage.open", "Engine.start", True),  # typed field
        ("Garage.open", "Engine.restart", True),  # typed parameter
        ("Garage.open", "Door.unlock", False),  # typed named parameter, unknown type
        ("Garage.open", "Window", False),  # instantiation
        ("Garage.open", "Window.close", False),  # typed local
        ("Garage.open", "Engine", True),
        ("Garage.open", "Engine.stop", True),  # cascade on typed field
        ("Garage.open", "Box", False),  # generic invocation misparse
        ("Garage.open", "Item", False),  # its type argument
        ("Garage.open", "Crate", False),  # new expression
        ("Garage.open", "Label", False),  # const object expression
        ("Garage.open", "Car", False),  # import prefix stripped
        ("Garage.open", "build", True),  # same-file top-level function
        ("build", "Engine", True),
        ("Garage", "Engine", True),  # field type
    }
    assert expected_present <= edges
    callees = {callee for _, callee, _ in edges}
    assert "m" not in callees and "m.Car" not in callees
    assert "unknown.call" not in callees and "call" not in callees
    assert "inferred.stop" not in callees  # untyped local: unknown receiver


def test_relationships_are_deduplicated(tmp_path: Path) -> None:
    source = "class A {}\nclass B {\n  void f() { A(); A(); }\n}\n"
    _, relationships = _analyze(tmp_path, source, "lib/dup.dart")
    keys = [(r.caller, r.callee, r.call_line) for r in relationships]
    assert len(keys) == len(set(keys))


def test_extension_bare_calls(tmp_path: Path) -> None:
    source = "class Api {}\nextension X on Api {\n  void g() { get(); helperFn(); }\n}\n"
    edges = _edges(_analyze(tmp_path, source, "lib/ext.dart")[1])
    assert ("X.g", "Api.get", False) in edges
    assert ("X.g", "helperFn", False) in edges
    assert ("X.g", "get", False) in edges


def test_extension_on_core_type_emits_nothing(tmp_path: Path) -> None:
    source = "extension on String {\n  String s() => trim();\n}\n"
    edges = _edges(_analyze(tmp_path, source, "lib/core_ext.dart")[1])
    assert not {e for e in edges if e[0] == "extension_on_String.s"}


def test_this_field_method_call(tmp_path: Path) -> None:
    source = (
        "class Api { void get() {} }\n"
        "class A {\n  final Api api;\n  A(this.api);\n  void f() { this.api.get(); }\n}\n"
    )
    edges = _edges(_analyze(tmp_path, source, "lib/thisf.dart")[1])
    assert ("A.f", "Api.get", True) in edges


def _write_repo(tmp_path: Path, files: dict[str, str]) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    for relpath, source in files.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_imported_definition_wins_over_global_duplicate(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/a/home.dart": (
                "import 'package:demo/a/widgets.dart';\n"
                "class HomePage {\n  void show() { InfoCard(); }\n}\n"
            ),
            "lib/a/widgets.dart": "class InfoCard {}\n",
            "lib/b/other.dart": "class InfoCard {}\n",
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    deps = components["lib/a/home.dart::HomePage.show"].depends_on
    assert "lib/a/widgets.dart::InfoCard" in deps
    assert "lib/b/other.dart::InfoCard" not in deps


def test_private_names_bind_within_library_only(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/c/lib.dart": "part 'part.dart';\nclass X {\n  void f() { _helper(); }\n}\n",
            "lib/c/part.dart": "part of 'lib.dart';\nvoid _helper() {}\nvoid _onlyInC() {}\n",
            "lib/d/z.dart": (
                "void _helper() {}\nclass Z {\n  void g() { _helper(); _onlyInC(); }\n}\n"
            ),
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert components["lib/c/lib.dart::X.f"].depends_on == {"lib/c/part.dart::_helper"}
    # Z.g sees its own _helper; _onlyInC is private to library c and must not bind.
    assert components["lib/d/z.dart::Z.g"].depends_on == {"lib/d/z.dart::_helper"}


def test_dotted_callee_falls_back_to_visible_type(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/api.dart": "class ApiClient {\n  void get() {}\n}\n",
            "lib/other_api.dart": "class Other {\n  void get() {}\n}\n",
            "lib/repo.dart": (
                "import 'api.dart';\n"
                "class Repo {\n  final ApiClient api;\n  Repo(this.api);\n"
                "  void load() { api.get(); api.post(); }\n}\n"
            ),
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    deps = components["lib/repo.dart::Repo.load"].depends_on
    assert "lib/api.dart::ApiClient.get" in deps
    assert "lib/api.dart::ApiClient" in deps  # api.post(): no such method -> the type
    assert "lib/other_api.dart::Other.get" not in deps


def test_inherited_mixin_call_resolves_cross_file(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {
            "lib/log.dart": "mixin Loggable {\n  void log(String m) {}\n}\n",
            "lib/svc.dart": (
                "import 'log.dart';\nclass Svc with Loggable {\n  void run() { log('x'); }\n}\n"
            ),
        },
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert "lib/log.dart::Loggable.log" in components["lib/svc.dart::Svc.run"].depends_on
    assert "lib/log.dart::Loggable" in components["lib/svc.dart::Svc"].depends_on
