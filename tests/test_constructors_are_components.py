"""A class's constructor is a component like any method, in every object-oriented analyzer.

It often does the real work (node-semver's ``SemVer`` constructor parses the whole version string), and calls
in its body need it as their caller: JS and Ruby registered the constructor only for call resolution, so its
calls had a caller that was not a component and their edges were dropped; Java, C#, Kotlin and Dart gave
them to the class. Python (``__init__``), PHP (``__construct``), C++ (``A.A``) and Scala (``A.this``) already
emitted it as a method.
"""

import pytest

from codewiki.src.be.dependency_analyzer.analyzers.csharp import analyze_csharp_file
from codewiki.src.be.dependency_analyzer.analyzers.dart import TreeSitterDartAnalyzer
from codewiki.src.be.dependency_analyzer.analyzers.java import analyze_java_file
from codewiki.src.be.dependency_analyzer.analyzers.javascript import (
    analyze_javascript_file_treesitter,
)
from codewiki.src.be.dependency_analyzer.analyzers.kotlin import TreeSitterKotlinAnalyzer
from codewiki.src.be.dependency_analyzer.analyzers.ruby import analyze_ruby_file
from codewiki.src.be.dependency_analyzer.analyzers.typescript import (
    analyze_typescript_file_treesitter,
)


def _kotlin(path, src, repo):
    a = TreeSitterKotlinAnalyzer(path, src, repo)
    return a.nodes, a.call_relationships


def _dart(path, src, repo):
    a = TreeSitterDartAnalyzer(path, src, repo)
    return a.nodes, a.call_relationships


CASES = {
    "javascript": (
        analyze_javascript_file_treesitter,
        "semver.js",
        """function parse(v) { return v }
class SemVer {
  constructor(version) { this.raw = parse(version) }
  format() { return this.raw }
}
""",
        "SemVer.constructor",
        "parse",
    ),
    "typescript": (
        analyze_typescript_file_treesitter,
        "struct.ts",
        """function check(x: number): number { return x }
export class Struct {
  value: number
  constructor(v: number) { this.value = check(v) }
}
""",
        "Struct.constructor",
        "check",
    ),
    "ruby": (
        analyze_ruby_file,
        "color.rb",
        """def parse(x) x end
class Color
  def initialize(x)
    @v = parse(x)
  end
end
""",
        "Color.initialize",
        "parse",
    ),
    "java": (
        analyze_java_file,
        "A.java",
        """class A {
  private int v;
  A(int x) { v = parse(x); }
  int parse(int x) { return x; }
}
""",
        "A.A",
        "parse",
    ),
    "csharp": (
        analyze_csharp_file,
        "A.cs",
        """class A {
  int v;
  public A(int x) { v = Parse(x); }
  int Parse(int x) { return x; }
}
""",
        "A.A",
        "Parse",
    ),
    "kotlin": (
        _kotlin,
        "A.kt",
        """class A(val x: Int) {
  var v = 0
  constructor(s: String) : this(s.length) { v = parse(x) }
  fun parse(x: Int): Int = x
}
""",
        "A.constructor",
        "parse",
    ),
    "dart": (
        _dart,
        "a.dart",
        """class A {
  int v;
  A(int x) : v = 0 { v = parse(x); }
  static int parse(x) => 0;
}
""",
        "A.A",
        "parse",
    ),
}


@pytest.mark.parametrize("lang", sorted(CASES))
def test_a_constructor_is_a_method_component_and_the_caller_of_its_calls(lang, tmp_path):
    analyze, name, src, ctor, callee = CASES[lang]
    path = tmp_path / name
    path.write_text(src)
    nodes, edges = analyze(str(path), src, str(tmp_path))
    by_name = {n.name: n for n in nodes}
    assert ctor in by_name, sorted(by_name)
    assert by_name[ctor].component_type in ("method", "function")
    assert by_name[ctor].id == f"{name}::{ctor}"
    callers = {e.caller for e in edges if e.callee.split(".")[-1].split("::")[-1] == callee}
    assert f"{name}::{ctor}" in callers, sorted((e.caller, e.callee) for e in edges)


def test_kotlin_init_blocks_are_components(tmp_path):
    src = """class A(val x: Int) {
  var v = 0
  init { v = parse(x) }
  fun parse(x: Int): Int = x
}
"""
    nodes, edges = _kotlin(str(tmp_path / "A.kt"), src, str(tmp_path))
    assert "A.init" in {n.name for n in nodes}
    assert any(e.caller == "A.kt::A.init" and e.callee.endswith("parse") for e in edges)


def test_a_dart_constructor_with_no_code_of_its_own_stays_with_its_class(tmp_path):
    src = """class A {
  int v;
  A(this.v);
  const A.c(this.v);
  factory A.redir(int x) = A;
  factory A.of(int x) => A(x);
}
"""
    names = {n.name for n in _dart(str(tmp_path / "a.dart"), src, str(tmp_path))[0]}
    assert "A.of" in names
    assert not names & {"A.A", "A.c", "A.redir"}
