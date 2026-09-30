"""Tests for the tree-sitter based Rust analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_rust")

from codewiki.src.be.dependency_analyzer.analyzers.artifact import (
    ArtifactOptions,
    classify_artifact,
)
from codewiki.src.be.dependency_analyzer.analyzers.rust import analyze_rust_file
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

SAMPLE = """\
use std::collections::HashMap;

/// Base contract for buffers that can be flushed.
pub trait Flushable {
    fn flush(&mut self);
}

/// Structured logger used by buffers.
pub struct Logger;

impl Logger {
    pub fn new() -> Self {
        Logger
    }

    pub fn warn(&self, message: &str) {}
}

pub struct Event {
    pub id: u64,
}

pub enum Signal {
    Stop,
    Emit(Event),
}

/// Buffers events before flushing them downstream.
#[derive(Debug, Clone)]
pub struct Buffer<T> {
    capacity: usize,
    logger: Logger,
    items: Vec<T>,
    index: HashMap<u64, Event>,
}

impl<T> Buffer<T> {
    pub fn new(capacity: usize) -> Self {
        Self::with_logger(capacity, Logger::new())
    }

    fn with_logger(capacity: usize, logger: Logger) -> Self {
        Buffer { capacity, logger, items: Vec::new(), index: HashMap::new() }
    }

    pub fn push(&mut self, item: T) {
        self.validate();
        self.logger.warn("pushed");
        let local = Logger::new();
        local.warn("local");
        let event = Event { id: 1 };
        record(&event);
        external_service::ping();
        self.items.push(item);
        std::mem::drop(event);
    }

    fn validate(&self) -> bool {
        self.items.len().checked_add(1).unwrap() <= self.capacity
    }
}

impl<T> Flushable for Buffer<T> {
    fn flush(&mut self) {
        self.items.clear();
    }
}

pub fn record(event: &Event) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pushes() {
        let mut buffer: Buffer<u8> = Buffer::new(4);
        buffer.push(1);
    }
}
"""


def _analyze(tmp_path: Path, source: str = SAMPLE, name: str = "buffer.rs"):
    file_path = tmp_path / name
    file_path.write_text(source, encoding="utf-8")
    return analyze_rust_file(str(file_path), source, repo_path=str(tmp_path))


def _edges(relationships) -> set[tuple[str, str]]:
    return {(r.caller, r.callee) for r in relationships}


def test_extracts_structs_enums_traits_and_methods(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Buffer"].component_type == "struct"
    assert by_name["Buffer"].node_type == "struct"
    assert by_name["Flushable"].component_type == "interface"
    assert by_name["Flushable"].node_type == "trait"
    assert by_name["Signal"].component_type == "class"
    assert by_name["Signal"].node_type == "enum"

    # Trait signature and impl methods are owned by their type.
    assert by_name["Flushable.flush"].component_type == "method"
    assert by_name["Buffer.flush"].class_name == "Buffer"
    assert by_name["Buffer.new"].component_type == "method"
    assert by_name["Logger.warn"].class_name == "Logger"

    assert by_name["record"].component_type == "function"
    assert by_name["Buffer"].id == "buffer.rs::Buffer"
    assert all(node.language == "rust" for node in nodes)


def test_trait_impl_is_recorded_as_base_class(tmp_path: Path) -> None:
    nodes, relationships = _analyze(tmp_path)
    buffer = next(n for n in nodes if n.name == "Buffer")
    assert buffer.base_classes == ["Flushable"]
    assert ("buffer.rs::Buffer", "buffer.rs::Flushable") in _edges(relationships)


def test_extracts_docstring_across_attributes_and_parameters(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert by_name["Buffer"].docstring == "/// Buffers events before flushing them downstream."
    assert by_name["Flushable"].has_docstring
    assert not by_name["Event"].has_docstring
    assert by_name["Logger.warn"].parameters == ["&self", "message: &str"]


def test_cfg_test_module_is_skipped(tmp_path: Path) -> None:
    nodes, relationships = _analyze(tmp_path)
    assert not any("pushes" in node.name for node in nodes)
    assert not any("pushes" in r.caller for r in relationships)


def test_type_edges(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    # Struct field and generic-argument field types.
    assert ("buffer.rs::Buffer", "buffer.rs::Logger") in edges
    assert ("buffer.rs::Buffer", "buffer.rs::Event") in edges
    # Tuple enum variant payload.
    assert ("buffer.rs::Signal", "buffer.rs::Event") in edges
    # Generic parameters and std types never become edges.
    assert not any(callee in ("T", "Vec", "HashMap", "u64") for _, callee in edges)


def test_call_edges(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    edges = _edges(relationships)

    # Self:: and Type:: path calls.
    assert ("buffer.rs::Buffer.new", "buffer.rs::Buffer.with_logger") in edges
    assert ("buffer.rs::Buffer.new", "buffer.rs::Logger.new") in edges
    # Struct literal instantiation.
    assert ("buffer.rs::Buffer.with_logger", "buffer.rs::Buffer") in edges
    assert ("buffer.rs::Buffer.push", "buffer.rs::Event") in edges
    # self.method(), self.field.method(), and a typed local's method.
    assert ("buffer.rs::Buffer.push", "buffer.rs::Buffer.validate") in edges
    assert ("buffer.rs::Buffer.push", "buffer.rs::Logger.warn") in edges
    # Free function in the same file.
    assert ("buffer.rs::Buffer.push", "buffer.rs::record") in edges


def test_noise_and_std_calls_are_dropped(tmp_path: Path) -> None:
    _, relationships = _analyze(tmp_path)
    callees = {r.callee for r in relationships}

    for noise in ("unwrap", "len", "clear", "push", "drop", "Vec.new", "HashMap.new"):
        assert noise not in callees
    assert not any(c.startswith("std") for c in callees)
    # Module-qualified calls keep only the function name, unresolved.
    unresolved = {r.callee for r in relationships if not r.is_resolved}
    assert "ping" in unresolved
    assert "checked_add" in unresolved


def test_inline_modules_prefix_logical_names(tmp_path: Path) -> None:
    source = """\
mod alpha {
    pub fn run() { helper(); }
    fn helper() {}
}
mod beta {
    pub fn run() {}
}
"""
    nodes, relationships = _analyze(tmp_path, source, "mods.rs")
    names = {node.name for node in nodes}
    assert {"alpha.run", "alpha.helper", "beta.run"} <= names
    assert ("mods.rs::alpha.run", "mods.rs::alpha.helper") in _edges(relationships)


def test_empty_and_broken_files_return_no_components(tmp_path: Path) -> None:
    assert _analyze(tmp_path, "", "empty.rs") == ([], [])
    nodes, _ = _analyze(tmp_path, "fn broken( {{{ struct", "broken.rs")
    assert isinstance(nodes, list)


def test_build_rs_classifies_as_build_artifact() -> None:
    opts = ArtifactOptions()
    assert classify_artifact("build.rs", "build.rs", 100, opts) == "build"
    assert classify_artifact("Cargo.toml", "Cargo.toml", 100, opts) == "manifest"
    assert classify_artifact("src/lib.rs", "lib.rs", 100, opts) is None


def test_dependency_parser_end_to_end(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "util.rs").write_text(
        "pub struct Config { pub name: String }\n\npub fn helper(config: &Config) {}\n",
        encoding="utf-8",
    )
    (src / "lib.rs").write_text(
        "mod util;\n"
        "use crate::util::Config;\n\n"
        "pub struct App { config: Config }\n\n"
        "impl App {\n"
        "    pub fn run(&self) { util::helper(&self.config); }\n"
        "}\n",
        encoding="utf-8",
    )

    components = DependencyParser(str(tmp_path)).parse_repository()

    assert "src/lib.rs::App" in components
    assert "src/lib.rs::App.run" in components
    assert "src/util.rs::Config" in components

    # Cross-file field-type and module-qualified call edges resolve globally.
    assert "src/util.rs::Config" in components["src/lib.rs::App"].depends_on
    assert "src/util.rs::helper" in components["src/lib.rs::App.run"].depends_on
