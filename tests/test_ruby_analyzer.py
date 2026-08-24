"""Tests for the tree-sitter based Ruby analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_ruby")

from codewiki.src.be.dependency_analyzer.analyzers.ruby import analyze_ruby_file
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

SAMPLE = """\
require "json"
require_relative "helpers/formatter"

module Pipeline
  # Buffers events before flushing them downstream.
  class Buffer < BaseBuffer
    include Enumerable
    include Flushable

    def initialize(size)
      @size = size
      @items = []
    end

    def push(event)
      validate(event)
      @items.push(event)
      Formatter.render(event)
    end

    def validate(event)
      raise ArgumentError if event.nil?
    end

    def self.build(size)
      Buffer.new(size)
    end
  end

  def self.default_buffer
    Buffer.new(10)
  end
end

def standalone_helper(value)
  value.to_s
end
"""


def _analyze(tmp_path: Path):
    file_path = tmp_path / "buffer.rb"
    file_path.write_text(SAMPLE, encoding="utf-8")
    return analyze_ruby_file(str(file_path), SAMPLE, repo_path=str(tmp_path))


def test_extracts_modules_classes_and_methods(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Pipeline"].component_type == "module"
    assert by_name["Buffer"].component_type == "class"
    assert by_name["Buffer"].base_classes == ["BaseBuffer"]
    assert by_name["Buffer.push"].component_type == "method"
    assert by_name["Buffer.push"].class_name == "Buffer"
    assert by_name["Buffer.build"].component_type == "method"
    assert by_name["Pipeline.default_buffer"].component_type == "method"
    assert by_name["standalone_helper"].component_type == "function"

    # Constructors are not documentable components.
    assert "Buffer.initialize" not in by_name

    assert by_name["Buffer"].id == "buffer.rb::Buffer"
    assert by_name["Buffer.push"].id == "buffer.rb::Buffer.push"
    assert all(node.language == "ruby" for node in nodes)


def test_extracts_docstring_and_parameters(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Buffer"].has_docstring
    assert "Buffers events" in by_name["Buffer"].docstring
    assert by_name["Buffer.push"].parameters == ["event"]


def test_extracts_call_relationships(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = {(rel.caller, rel.callee, rel.is_resolved) for rel in relationships}

    # Inheritance and mixins (BaseBuffer / Flushable live in another file).
    assert ("buffer.rb::Buffer", "BaseBuffer", False) in edges
    assert ("buffer.rb::Buffer", "Flushable", False) in edges
    # Builtin mixins are dropped.
    assert not any(rel.callee == "Enumerable" for rel in relationships)

    # Intra-class implicit-self call resolves to the sibling method.
    assert ("buffer.rb::Buffer.push", "buffer.rb::Buffer.validate", True) in edges
    # Constant-receiver call to an out-of-file class stays a bare logical name.
    assert ("buffer.rb::Buffer.push", "Formatter.render", False) in edges
    # Instantiation points at the class, resolved within the same file.
    assert ("buffer.rb::Buffer.build", "buffer.rb::Buffer", True) in edges
    assert ("buffer.rb::Pipeline.default_buffer", "buffer.rb::Buffer", True) in edges

    # Kernel noise like `raise` and `to_s` is not emitted.
    assert not any(rel.callee.endswith("raise") for rel in relationships)
    assert not any(rel.callee.endswith("to_s") for rel in relationships)


def test_dependency_parser_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "base_buffer.rb").write_text(
        "class BaseBuffer\n  def flush\n  end\nend\n", encoding="utf-8"
    )
    (tmp_path / "buffer.rb").write_text(SAMPLE, encoding="utf-8")

    components = DependencyParser(str(tmp_path)).parse_repository()

    assert "buffer.rb::Buffer" in components
    assert "buffer.rb::Buffer.push" in components
    assert "base_buffer.rb::BaseBuffer" in components

    # The cross-file inheritance edge resolves during global resolution.
    assert "base_buffer.rb::BaseBuffer" in components["buffer.rb::Buffer"].depends_on
    # The intra-file call edge survives into depends_on.
    assert "buffer.rb::Buffer.validate" in components["buffer.rb::Buffer.push"].depends_on
