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
