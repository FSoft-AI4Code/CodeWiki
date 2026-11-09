"""Repository-local Lean name resolution, including scoped opens and exports.

Commands are bound in import/source order. Bindings retain their defining file
and position: importing a target alone must not expose an unrelated export.
No type elaboration, external declarations, or notation expansion is attempted.
"""

from collections import defaultdict
from dataclasses import dataclass

from codewiki.src.be.dependency_analyzer.analyzers.lean import (
    LeanFile,
    LeanOpen,
    LeanScope,
    _qualify,
)
from codewiki.src.be.dependency_analyzer.models.core import Node

NO_MATCH = object()


@dataclass(frozen=True)
class _Binding:
    target: str
    origin: str
    line: int
    protected: bool = False
    exported: bool = False


def _prefixes(name: str) -> list[str]:
    parts = name.split(".") if name else []
    return [".".join(parts[:end]) for end in range(len(parts), 0, -1)] + [""]


class LeanNameResolver:
    def __init__(
        self,
        functions: dict[str, Node],
        files: dict[str, LeanFile],
        scopes: dict[str, LeanScope],
        aliases: dict[str, list[str]],
        private: set[str],
        protected: set[str],
    ):
        self.functions = functions
        self.files = files
        self.scopes = scopes
        self.private = private
        self.index: dict[str, list[_Binding]] = defaultdict(list)
        # Earliest introduction per file is enough to test command visibility.
        self.namespaces: dict[str, dict[str, int]] = defaultdict(dict)
        self.modules: dict[str, set[str]] = defaultdict(set)
        self.visible: dict[str, frozenset[str]] = {}
        self.open_targets: dict[tuple[str, int], tuple[str, ...]] = {}

        for rel, scanned in files.items():
            parts = rel.removesuffix(".lean").split("/")
            for start in range(len(parts)):
                self.modules[".".join(parts[start:])].add(rel)
            for line, namespace in scanned.namespaces:
                self._add_namespace(namespace, rel, line)

        # Declaration names are indexed as before; directive binding additionally
        # filters by position so a later namespace cannot change an earlier open.
        for node_id, node in functions.items():
            if node.language != "lean" or not node.qualified_name:
                continue
            scope = scopes.get(node_id)
            line = scope.line if scope else node.start_line - 1
            self._add_binding(
                node.qualified_name,
                _Binding(node_id, node.relative_path, line, node_id in protected),
            )
        for name, targets in aliases.items():
            for target in targets:
                node = functions[target]
                scope = scopes.get(target)
                self._add_binding(
                    name,
                    _Binding(
                        target,
                        node.relative_path,
                        scope.line if scope else node.start_line - 1,
                        target in protected,
                    ),
                )

        done: set[str] = set()
        visiting: set[str] = set()

        def bind_file(rel: str) -> None:
            if rel in done or rel in visiting:
                return  # invalid import cycles must not abort repository analysis
            visiting.add(rel)
            scanned = files[rel]
            for module in scanned.imports:
                for dependency in sorted(self.modules.get(module, ())):
                    bind_file(dependency)
            events = [(opening.line, 0, i) for i, opening in enumerate(scanned.opens)]
            events += [(export.line, 1, i) for i, export in enumerate(scanned.exports)]
            for _, kind, i in sorted(events):
                if kind == 0:
                    self._bind_open(rel, i)
                else:
                    export = scanned.exports[i]
                    namespaces = self._namespace_targets(
                        export.namespace, export.destination, export.opens, rel, export.line
                    )
                    for name in export.names:
                        targets = {
                            binding.target
                            for namespace in namespaces
                            for binding in self._matches(
                                _qualify(namespace, name), rel, export.line
                            )
                        }
                        if len(targets) == 1:
                            self._add_binding(
                                _qualify(export.destination, name),
                                _Binding(next(iter(targets)), rel, export.line, exported=True),
                            )
            visiting.remove(rel)
            done.add(rel)

        for rel in sorted(files):
            bind_file(rel)

    def _add_binding(self, name: str, binding: _Binding) -> None:
        if binding not in self.index[name]:
            self.index[name].append(binding)
        for namespace in _prefixes(name)[1:-1]:
            self._add_namespace(namespace, binding.origin, binding.line)

    def _add_namespace(self, namespace: str, origin: str, line: int) -> None:
        introductions = self.namespaces[namespace]
        introductions[origin] = min(line, introductions.get(origin, line))

    def _visible_files(self, rel: str) -> frozenset[str]:
        if rel not in self.visible:
            seen = {rel}
            queue = [rel]
            while queue:
                scanned = self.files.get(queue.pop())
                if scanned is None:
                    continue
                for module in scanned.imports:
                    for dependency in self.modules.get(module, ()):
                        if dependency not in seen:
                            seen.add(dependency)
                            queue.append(dependency)
            self.visible[rel] = frozenset(seen)
        return self.visible[rel]

    def _matches(
        self, name: str, rel: str, line: int, *, declarations_after: bool = False
    ) -> list[_Binding]:
        visible = self._visible_files(rel)
        return [
            binding
            for binding in self.index.get(name, ())
            if binding.origin in visible
            and self.functions[binding.target].relative_path in visible
            and (
                binding.origin != rel
                or binding.line <= line
                or declarations_after
                and not binding.exported
            )
            and (
                binding.target not in self.private
                or self.functions[binding.target].relative_path == rel
            )
        ]

    @staticmethod
    def _opened_name(opening: LeanOpen, name: str) -> str | None:
        if opening.mode == "scoped":
            return None
        if opening.mode == "select":
            return name if name in opening.names else None
        if opening.mode == "hiding":
            return None if name in opening.names else name
        if opening.mode == "renaming":
            return next((source for source, alias in opening.renames if alias == name), None)
        return name

    def _namespace_targets(
        self, name: str, context: str, opens: tuple[int, ...], rel: str, line: int
    ) -> tuple[str, ...]:
        visible = self._visible_files(rel)

        def exists(namespace: str) -> bool:
            return any(
                origin in visible and (origin != rel or position <= line)
                for origin, position in self.namespaces.get(namespace, {}).items()
            )

        if name.startswith("_root_."):
            target = name[len("_root_.") :]
            return (target,) if exists(target) else ()
        candidates = {_qualify(prefix, name) for prefix in _prefixes(context)}
        for opening_id in opens:
            opening = self.files[rel].opens[opening_id]
            suffix = self._opened_name(opening, name)
            if suffix is not None:
                candidates.update(
                    _qualify(namespace, suffix)
                    for namespace in self.open_targets.get((rel, opening_id), ())
                )
        return tuple(sorted(candidate for candidate in candidates if exists(candidate)))

    def _bind_open(self, rel: str, opening_id: int) -> None:
        opening = self.files[rel].opens[opening_id]
        targets = self._namespace_targets(
            opening.namespace, opening.context, opening.preceding, rel, opening.line
        )
        # `hiding` and `renaming` require one unambiguous namespace.
        if opening.mode in ("hiding", "renaming") and len(targets) != 1:
            targets = ()
        self.open_targets[rel, opening_id] = targets

    def resolve(self, callee: str, caller: Node) -> str | None:
        """Find the longest constant prefix, preserving ambiguity as a stop."""
        parts = callee.split(".")
        for end in range(len(parts), 0, -1):
            found = self._resolve_name(".".join(parts[:end]), caller)
            if found is not NO_MATCH:
                return found
        return None

    def _resolve_name(self, name: str, caller: Node):
        rel = caller.relative_path
        scope = self.scopes.get(caller.id, LeanScope((), (), caller.start_line - 1))

        def matches(fqn: str) -> list[_Binding]:
            # Keep the existing declaration lookup behavior (including mutual
            # definitions), but never expose an export before its command.
            return self._matches(fqn, rel, scope.line, declarations_after=True)

        def targets(fqn: str) -> set[str]:
            return {binding.target for binding in matches(fqn)}

        def unique(found: set[str]) -> str | None:
            return next(iter(found)) if len(found) == 1 and caller.id not in found else None

        if name.startswith("_root_."):
            found = targets(name[len("_root_.") :])
            return unique(found) if found else NO_MATCH
        for prefix in (*scope.namespaces, ""):
            found = targets(_qualify(prefix, name))
            if found:
                return unique(found)
        opened: set[str] = set()
        for opening_id in scope.opens:
            opening = self.files[rel].opens[opening_id]
            suffix = self._opened_name(opening, name)
            if suffix is None:
                continue
            for namespace in self.open_targets.get((rel, opening_id), ()):
                for binding in matches(_qualify(namespace, suffix)):
                    if opening.mode in ("select", "renaming") or not binding.protected:
                        opened.add(binding.target)
        return unique(opened) if opened else NO_MATCH
