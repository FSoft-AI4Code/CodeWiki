"""Tests for the tree-sitter based Scala analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_scala")

from codewiki.src.be.dependency_analyzer.analyzers.artifact import (
    ArtifactOptions,
    classify_artifact,
)
from codewiki.src.be.dependency_analyzer.analyzers.scala import analyze_scala_file
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

SAMPLE = """\
package pipeline

/** Base contract for buffers that can be flushed. */
trait Flushable {
  def flush(): Unit
}

/** Structured logger used by buffers. */
class Logger {
  def warn(message: String): Unit = ()
}

/** Buffers events before flushing them downstream. */
class Buffer(capacity: Int, logger: Logger) extends BaseBuffer with Flushable {

  def push(item: Int): Unit = {
    validate(item)
    logger.warn("pushed")
    ExternalService.ping()
  }

  def validate(item: Int): Unit = {
    require(item >= 0)
  }

  def flush(): Unit = {
    println("flushing")
  }
}

/** Companion object for Buffer. */
object Buffer {
  def apply(capacity: Int): Buffer = new Buffer(capacity, new Logger())
}

def standaloneHelper(value: Int): Int = value + 1
"""

SCALA3_SAMPLE = """\
package shapes

enum Color:
  case Red, Green, Blue

trait Shape:
  def area: Double

case class Circle(radius: Double) extends Shape:
  def area: Double = Math.PI * radius * radius
"""


def _analyze(tmp_path: Path):
    file_path = tmp_path / "buffer.scala"
    file_path.write_text(SAMPLE, encoding="utf-8")
    return analyze_scala_file(str(file_path), SAMPLE, repo_path=str(tmp_path))


def test_extracts_traits_classes_objects_and_methods(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Flushable"].component_type == "interface"
    assert by_name["Flushable"].node_type == "trait"

    assert by_name["Buffer"].component_type == "class"
    assert by_name["Buffer"].node_type == "class"
    assert by_name["Buffer"].base_classes == ["BaseBuffer", "Flushable"]

    assert by_name["Buffer.push"].component_type == "method"
    assert by_name["Buffer.push"].class_name == "Buffer"
    assert by_name["Buffer.validate"].component_type == "method"
    assert by_name["standaloneHelper"].component_type == "function"

    # Companion object survives as a distinct, `$`-suffixed component.
    assert by_name["Buffer$"].component_type == "class"
    assert by_name["Buffer$"].node_type == "object"
    assert by_name["Buffer$.apply"].component_type == "method"
    assert by_name["Buffer$.apply"].class_name == "Buffer$"

    assert by_name["Buffer"].id == "buffer.scala::Buffer"
    assert by_name["Buffer$"].id == "buffer.scala::Buffer$"
    assert by_name["Buffer.push"].id == "buffer.scala::Buffer.push"
    assert by_name["Buffer$.apply"].id == "buffer.scala::Buffer$.apply"
    assert all(node.language == "scala" for node in nodes)


def test_extracts_docstring_and_parameters(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Buffer"].has_docstring
    assert "Buffers events" in by_name["Buffer"].docstring
    assert by_name["Buffer.push"].parameters == ["item: Int"]


def test_extracts_call_relationships(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = {(rel.caller, rel.callee, rel.is_resolved) for rel in relationships}

    # Inheritance: BaseBuffer lives in another file, stays an unresolved logical name.
    assert ("buffer.scala::Buffer", "BaseBuffer", False) in edges
    # Trait mixin resolved within the same file.
    assert ("buffer.scala::Buffer", "buffer.scala::Flushable", True) in edges

    # Constructor parameter of a repository-local type.
    assert ("buffer.scala::Buffer", "buffer.scala::Logger", True) in edges

    # Intra-type call resolves to the sibling method.
    assert ("buffer.scala::Buffer.push", "buffer.scala::Buffer.validate", True) in edges
    # Call through a constructor-parameter-typed receiver resolves.
    assert ("buffer.scala::Buffer.push", "buffer.scala::Logger.warn", True) in edges
    # External symbol stays an unresolved logical name.
    assert ("buffer.scala::Buffer.push", "ExternalService.ping", False) in edges

    # Instantiation edges from the companion's factory method.
    assert ("buffer.scala::Buffer$.apply", "buffer.scala::Buffer", True) in edges
    assert ("buffer.scala::Buffer$.apply", "buffer.scala::Logger", True) in edges

    # Standard-library noise is excluded.
    assert not any(rel.callee.endswith("require") for rel in relationships)
    assert not any(rel.callee.endswith("println") for rel in relationships)


def test_scala3_enum_and_indentation_syntax(tmp_path: Path) -> None:
    file_path = tmp_path / "shapes.scala"
    file_path.write_text(SCALA3_SAMPLE, encoding="utf-8")
    nodes, _ = analyze_scala_file(str(file_path), SCALA3_SAMPLE, repo_path=str(tmp_path))
    by_name = {node.name: node for node in nodes}

    assert by_name["Color"].component_type == "class"
    assert by_name["Color"].node_type == "enum"
    assert by_name["Shape"].component_type == "interface"
    assert by_name["Circle"].component_type == "class"
    assert by_name["Circle"].base_classes == ["Shape"]
    assert by_name["Circle.area"].component_type == "method"


def test_empty_file_returns_no_components(tmp_path: Path) -> None:
    file_path = tmp_path / "empty.scala"
    file_path.write_text("", encoding="utf-8")
    nodes, relationships = analyze_scala_file(str(file_path), "", repo_path=str(tmp_path))
    assert nodes == []
    assert relationships == []


def test_curried_method_parameters_are_not_truncated(tmp_path: Path) -> None:
    src = """\
class Curried {
  def add(x: Int)(y: Int): Int = x + y
}
"""
    file_path = tmp_path / "curried.scala"
    file_path.write_text(src, encoding="utf-8")
    nodes, _ = analyze_scala_file(str(file_path), src, repo_path=str(tmp_path))
    by_name = {node.name: node for node in nodes}

    # A curried method has one `parameters` node per group; both must survive.
    assert by_name["Curried.add"].parameters == ["x: Int", "y: Int"]


def test_parameterized_trait_constructor_edge_resolves(tmp_path: Path) -> None:
    src = """\
class Logger {
  def warn(message: String): Unit = ()
}

trait Parameterized(logger: Logger) {
  def use(): Unit = logger.warn("x")
}
"""
    file_path = tmp_path / "param_trait.scala"
    file_path.write_text(src, encoding="utf-8")
    _, relationships = analyze_scala_file(str(file_path), src, repo_path=str(tmp_path))
    edges = {(rel.caller, rel.callee, rel.is_resolved) for rel in relationships}

    # Scala 3 traits can take value parameters via the same `class_parameters`
    # shape as classes; the constructor-parameter edge must not be dropped.
    assert ("param_trait.scala::Parameterized", "param_trait.scala::Logger", True) in edges


def test_unresolved_non_noise_calls_stay_unresolved(tmp_path: Path) -> None:
    src = """\
class Worker {
  def run(param: Unknown): Unit = {
    doWork()
    param.process()
    val x = compute()
    x.finish()
  }
}
"""
    file_path = tmp_path / "worker.scala"
    file_path.write_text(src, encoding="utf-8")
    _, relationships = analyze_scala_file(str(file_path), src, repo_path=str(tmp_path))
    edges = {(rel.caller, rel.callee, rel.is_resolved) for rel in relationships}

    caller = "worker.scala::Worker.run"
    # Bare call with no receiver and no in-file match: unresolved bare name.
    assert (caller, "doWork", False) in edges
    # Receiver type resolves (constructor param), but the method itself doesn't.
    assert (caller, "Unknown.process", False) in edges
    # Receiver type can't be inferred at all: falls back to the bare method name.
    assert (caller, "finish", False) in edges


def test_sc_extension_reaches_the_scala_analyzer(tmp_path: Path) -> None:
    (tmp_path / "script.sc").write_text(
        'object Hello {\n  def greet(): String = "hi"\n}\n', encoding="utf-8"
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert "script.sc::Hello$" in components
    assert "script.sc::Hello$.greet" in components


def test_sbt_files_classify_as_build_artifacts() -> None:
    opts = ArtifactOptions()
    assert classify_artifact("build.sbt", "build.sbt", 100, opts) == "manifest"
    assert classify_artifact("project/plugins.sbt", "plugins.sbt", 50, opts) == "build"


def test_dependency_parser_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "base_buffer.scala").write_text(
        "package pipeline\n\nclass BaseBuffer {\n  def flush(): Unit = ()\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "buffer.scala").write_text(SAMPLE, encoding="utf-8")

    components = DependencyParser(str(tmp_path)).parse_repository()

    assert "buffer.scala::Buffer" in components
    assert "buffer.scala::Buffer.push" in components
    assert "base_buffer.scala::BaseBuffer" in components

    # The cross-file inheritance edge resolves during global resolution.
    assert "base_buffer.scala::BaseBuffer" in components["buffer.scala::Buffer"].depends_on
    # The intra-file call edge survives into depends_on.
    assert "buffer.scala::Buffer.validate" in components["buffer.scala::Buffer.push"].depends_on
