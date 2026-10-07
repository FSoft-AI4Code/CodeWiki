"""Tests for the Flutter-aware parts of the Dart analyzer."""

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from codewiki.src.be.dependency_analyzer.analyzers.dart import analyze_dart_file
from codewiki.src.be.dependency_analyzer.analyzers.dart_flutter import (
    classify_class,
    classify_top_level_initializer,
    provider_alias_candidates,
)
from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser

APP = """\
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

final carProvider = FutureProvider<Car>((ref) => ref.watch(repoProvider).fetch());
final counterProvider = NotifierProvider<CounterNotifier, int>(CounterNotifier.new);
final router = GoRouter(routes: [
  GoRoute(path: '/', builder: (c, s) => const HomePage()),
  GoRoute(path: '/settings', builder: (c, s) => SettingsPage()),
]);
final greeting = 'hi';

class CounterNotifier extends Notifier<int> {
  @override
  int build() => 0;
}

class HomePage extends ConsumerWidget {
  const HomePage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final car = ref.watch(carProvider);
    final n = ref.watch(counterProvider.notifier);
    return Scaffold(
      body: Column(children: [CarCard(car: car), BatteryGauge.small(), _buildFooter()]),
    );
  }

  Widget _buildFooter() => const Footer();
}

class CarCard extends StatelessWidget {
  Widget build(BuildContext context) => Text('car');
}

class Counter extends StatefulWidget {
  @override
  State<Counter> createState() => _CounterState();
}

class _CounterState extends State<Counter> {
  @override
  Widget build(BuildContext context) => CarCard();
}

class FancyCard extends CarCard {}
"""


def _analyze(tmp_path: Path, source: str = APP, name: str = "lib/app.dart"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return analyze_dart_file(str(path), source, repo_path=str(tmp_path))


def _edges(relationships):
    return {(r.caller.split("::")[-1], r.callee.split("::")[-1]) for r in relationships}


def test_classifiers() -> None:
    assert classify_class("StatelessWidget", set()) == "widget"
    assert classify_class("ConsumerStatefulWidget", set()) == "widget"
    assert classify_class("CarCard", {"CarCard"}) == "widget"
    assert classify_class("State", set()) == "state"
    assert classify_class("ConsumerState", set()) == "state"
    assert classify_class("Notifier", set()) == "notifier"
    assert classify_class("_$Counter", set()) is None
    assert classify_class("Bloc", set()) == "bloc"
    assert classify_class("HydratedCubit", set()) == "cubit"
    assert classify_class("Cubit", set()) == "cubit"
    assert classify_class("Repo", set()) is None
    assert classify_class(None, set()) is None
    assert classify_top_level_initializer(" FutureProvider<Car>((ref) => 1)") == "provider"
    assert classify_top_level_initializer(" StateProvider.autoDispose((ref) => 0)") == "provider"
    assert (
        classify_top_level_initializer(" FutureProvider.family<int, int>((ref, id) => id)")
        == "provider"
    )
    assert classify_top_level_initializer(" GoRouter(routes: [])") == "router"
    assert classify_top_level_initializer(" 'hi'") is None
    assert classify_top_level_initializer(" AuthProvider()") is None
    assert (
        classify_top_level_initializer(" NotifierProvider.autoDispose<A, int>(A.new)") == "provider"
    )
    assert (
        classify_top_level_initializer(" AutoDisposeFutureProvider<int>((ref) => 1)") == "provider"
    )
    assert provider_alias_candidates("cartProvider") == ["cart", "Cart", "CartNotifier"]
    assert provider_alias_candidates("Provider") == []
    assert provider_alias_candidates("cart") == []


def test_flutter_node_types(tmp_path: Path) -> None:
    nodes, _ = _analyze(tmp_path)
    types = {n.name: (n.component_type, n.node_type) for n in nodes}
    assert types["HomePage"] == ("class", "widget")
    assert types["CarCard"] == ("class", "widget")
    assert types["FancyCard"] == ("class", "widget")  # same-file widget subclass
    assert types["Counter"] == ("class", "widget")
    assert types["_CounterState"] == ("class", "state")
    assert types["CounterNotifier"] == ("class", "notifier")
    assert types["carProvider"] == ("function", "provider")
    assert types["counterProvider"] == ("function", "provider")
    assert types["router"] == ("function", "router")
    assert "greeting" not in types
    assert next(n for n in nodes if n.name == "HomePage").display_name == "widget HomePage"


def test_build_composition_is_lifted_to_widget_class(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    for child in ("CarCard", "BatteryGauge", "Footer", "Scaffold", "Column"):
        assert ("HomePage", child) in edges, child
    assert ("HomePage.build", "CarCard") in edges  # method-level edge kept
    assert ("Counter", "_CounterState") in edges  # createState pairing
    assert ("_CounterState", "Counter") in edges  # State<Counter> type argument
    assert ("_CounterState", "CarCard") in edges


def test_riverpod_and_router_edges(tmp_path: Path) -> None:
    edges = _edges(_analyze(tmp_path)[1])
    assert ("HomePage.build", "carProvider") in edges
    assert ("HomePage.build", "counterProvider") in edges  # .notifier stripped
    assert ("carProvider", "repoProvider") in edges
    assert ("carProvider", "Car") in edges
    assert ("counterProvider", "CounterNotifier") in edges  # type arg and tear-off
    assert ("router", "HomePage") in edges
    assert ("router", "SettingsPage") in edges
    callees = {callee for _, callee in edges}
    assert "ref.watch" not in callees and "watch" not in callees


def test_riverpod_codegen_alias_resolves(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "state.dart").write_text(
        "import 'package:riverpod_annotation/riverpod_annotation.dart';\n"
        "part 'state.g.dart';\n"
        "@riverpod\nclass Counter extends _$Counter {\n  @override\n  int build() => 0;\n}\n"
        "@riverpod\nclass CartNotifier extends _$CartNotifier {\n  int build() => 0;\n}\n"
        "@Riverpod(keepAlive: true)\nFuture<int> total(Ref ref) async => 1;\n"
        "Future<int> notAProvider() async => 1;\n"
        "class Cart extends ChangeNotifier {}\n"
        "class AppDatabase extends _$AppDatabase {}\n",
        encoding="utf-8",
    )
    (lib / "state.g.dart").write_text("part of 'state.dart';\n", encoding="utf-8")
    (lib / "view.dart").write_text(
        "import 'state.dart';\n"
        "class View extends ConsumerWidget {\n"
        "  Widget build(BuildContext context, WidgetRef ref) {\n"
        "    ref.watch(counterProvider);\n"
        "    ref.watch(totalProvider);\n"
        "    ref.watch(cartProvider);\n"  # Riverpod 3 naming
        "    ref.read(cartNotifierProvider);\n"  # Riverpod 2 naming
        "    ref.watch(notAProviderProvider);\n"
        "    ref.watch(appDatabaseProvider);\n"
        "    return const Placeholder();\n  }\n}\n",
        encoding="utf-8",
    )
    components = DependencyParser(str(tmp_path)).parse_repository()
    assert components["lib/state.dart::Counter"].node_type == "riverpod notifier"
    assert components["lib/state.dart::total"].node_type == "riverpod provider"
    deps = components["lib/view.dart::View.build"].depends_on
    assert {
        "lib/state.dart::Counter",
        "lib/state.dart::total",
        "lib/state.dart::CartNotifier",
    } <= deps
    # Only @riverpod-annotated declarations are alias targets.
    assert "lib/state.dart::notAProvider" not in deps
    assert "lib/state.dart::AppDatabase" not in deps  # `extends _$X` alone is not @riverpod
    assert "lib/state.dart::Cart" not in deps  # ChangeNotifier is not @riverpod
    assert components["lib/state.dart::AppDatabase"].node_type == "class"
