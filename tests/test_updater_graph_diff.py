"""Step 1 of the component-level updater: graph diff and rename pairing."""

from __future__ import annotations

from updater_toy import OAUTH, REFRESH, USER, VALIDATE, graph_r1, graph_r2, node

from codewiki.src.be.updater.graph_diff import (
    CLASS_ADDED,
    CLASS_BODY,
    CLASS_DELETED,
    CLASS_IFACE,
    CLASS_RENAMED,
    DIFF_TRUNCATED_MARKER,
    body_hash,
    diff_graphs,
)
from codewiki.src.be.updater.options import UpdateOptions


def test_toy_change_classes():
    d = diff_graphs(graph_r1(), graph_r2(), UpdateOptions())
    assert d.body == {VALIDATE}
    assert d.interface == {REFRESH}
    assert d.added == {OAUTH}
    assert d.deleted == {USER}
    assert d.renamed == {}
    assert d.records[VALIDATE].change_class == CLASS_BODY
    assert "+    log('validate')" in d.records[VALIDATE].diff
    assert d.records[REFRESH].change_class == CLASS_IFACE
    assert d.records[REFRESH].old_signature["parameters"] == ["self"]
    assert d.records[REFRESH].new_signature["parameters"] == ["self", "force"]
    assert d.records[OAUTH].change_class == CLASS_ADDED
    assert d.records[USER].change_class == CLASS_DELETED
    assert d.counts()["body"] == 1 and d.counts()["interface"] == 1


def test_unchanged_component_in_changed_file_is_absent():
    # login.py changed (validate) but login itself kept its hash.
    d = diff_graphs(graph_r1(), graph_r2())
    assert "src/auth/login.py::login" not in d.changed_ids


def test_whitespace_only_change_is_not_a_change():
    a = node("f.py::g", "function", "def g():\n    return 1\n")
    b = node("f.py::g", "function", "def g():\n\n    return   1\n")
    assert body_hash(a) == body_hash(b)
    assert diff_graphs({a.id: a}, {b.id: b}).is_empty


def test_edge_only_change_is_classified_as_edge():
    a = node("f.py::g", "function", "def g():\n    return h()\n")
    b = node("f.py::g", "function", "def g():\n    return h()\n", deps=["f.py::h"])
    d = diff_graphs({a.id: a}, {b.id: b})
    assert d.edge == {"f.py::g"}
    assert d.records["f.py::g"].edges_added == ["f.py::h"]


def test_rename_pairing_respects_tau_ren():
    body = "class Store:\n    def get(self, k):\n        return self.d[k]\n    def put(self, k, v):\n        self.d[k] = v\n"
    old = {"a.py::Store": node("a.py::Store", "class", body)}
    new = {"b.py::Store2": node("b.py::Store2", "class", body.replace("Store", "Store2"))}
    d = diff_graphs(old, new, UpdateOptions(tau_ren=0.9))
    assert d.renamed == {"a.py::Store": "b.py::Store2"}
    assert not d.added and not d.deleted
    assert d.records["b.py::Store2"].change_class == CLASS_RENAMED
    strict = diff_graphs(old, new, UpdateOptions(tau_ren=0.999))
    assert strict.renamed == {}
    assert strict.added == {"b.py::Store2"} and strict.deleted == {"a.py::Store"}


def test_rename_with_signature_change_is_also_interface():
    old = {"a.py::f": node("a.py::f", "function", "def f(x):\n    return x + 1\n", ["x"])}
    new = {"a.py::g": node("a.py::g", "function", "def g(x, y=0):\n    return x + 1\n", ["x", "y"])}
    d = diff_graphs(old, new, UpdateOptions(tau_ren=0.7))
    assert d.renamed == {"a.py::f": "a.py::g"}
    assert "a.py::g" in d.interface


def test_diff_truncation():
    big_old = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(3000))
    big_new = "def f():\n" + "".join(f"    x{i} = {i + 1}\n" for i in range(3000))
    old = {"a.py::f": node("a.py::f", "function", big_old)}
    new = {"a.py::f": node("a.py::f", "function", big_new)}
    d = diff_graphs(old, new, UpdateOptions(max_diff_tokens=50))
    assert DIFF_TRUNCATED_MARKER in d.records["a.py::f"].diff
