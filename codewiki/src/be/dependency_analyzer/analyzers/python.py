import ast
import logging
import os
import warnings
from typing import Dict, List, Optional, Set, Tuple

from codewiki.src.be.dependency_analyzer.models.core import Node, CallRelationship
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    PYTHON_OBJECT_METHODS,
    PYTHON_STDLIB_MODULES,
)

logger = logging.getLogger(__name__)


def _dotted_contains(project_module: str, target: str) -> bool:
    """Dotted-boundary containment: does `target` align with some segment run
    of `project_module`? Tolerates src-layouts where the import root differs
    from the repo root (import `pkg.util` vs project module `src.pkg.util`)."""
    return f".{target}." in f".{project_module}."


def is_project_import(target: str, project_modules: Set[str]) -> bool:
    return any(_dotted_contains(module, target) for module in project_modules)


class PythonASTAnalyzer(ast.NodeVisitor):
    def __init__(
        self,
        file_path: str,
        content: str,
        repo_path: Optional[str] = None,
        project_modules: Optional[Set[str]] = None,
    ):
        """
        Initialize the Python AST analyzer.

        Args:
            file_path: Path to the Python file being analyzed
            content: Raw content of the Python file
            repo_path: Repository root path for calculating relative paths
            project_modules: Dotted module paths of every Python file in the
                repository, used to classify imports as project vs third-party
        """
        self.file_path = file_path
        self.repo_path = repo_path
        self.content = content
        self.lines = content.splitlines()
        self.project_modules = project_modules
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []

        # Scope tracking: ("class"|"function", name) entries, plus the ids of
        # extracted components for caller attribution.
        self.scope_stack: List[Tuple[str, str]] = []
        self.component_stack: List[str] = []

        # Same-file definitions by bare name (top-level functions and classes).
        self.top_level_nodes: Dict[str, Node] = {}

        # Per-class knowledge, keyed by dotted class name ("Outer.Inner").
        self.class_methods: Dict[str, Set[str]] = {}
        self.class_bases: Dict[str, List[str]] = {}

        # Import bindings: local alias -> canonical dotted target.
        self.module_imports: Dict[str, str] = {}
        self.from_imports: Dict[str, str] = {}
        self.external_import_roots: Set[str] = set()

        # Shallow receiver knowledge: variable -> same-file class dotted name,
        # and variable -> dotted origin expression ("logging.getLogger").
        self.var_types: Dict[str, str] = {}
        self.var_origins: Dict[str, str] = {}

        # Names of functions nested inside the current function scopes. A
        # call to one is internal to its enclosing component, not an edge.
        self.local_function_names: List[Set[str]] = []

    def _get_relative_path(self) -> str:
        """Get relative path from repo root."""
        if self.repo_path:
            return os.path.relpath(self.file_path, self.repo_path)
        return str(self.file_path)

    def _get_module_path(self) -> str:
        try:
            path = self._get_relative_path()
            for ext in [".py", ".pyx"]:
                if path.endswith(ext):
                    path = path[: -len(ext)]
                    break
            module = path.replace("/", ".").replace("\\", ".")
        except Exception:
            module = str(self.file_path).replace("/", ".").replace("\\", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        return module

    def _current_class_dotted(self) -> Optional[str]:
        names = [name for kind, name in self.scope_stack if kind == "class"]
        return ".".join(names) if names else None

    def _inside_function(self) -> bool:
        return any(kind == "function" for kind, _ in self.scope_stack)

    def _caller_id(self) -> Optional[str]:
        return self.component_stack[-1] if self.component_stack else None

    def _add_relationship(self, callee: str, is_resolved: bool, line: int) -> None:
        caller = self._caller_id()
        if not caller:
            return
        self.call_relationships.append(
            CallRelationship(
                caller=caller,
                callee=callee,
                call_line=line,
                is_resolved=is_resolved,
            )
        )

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.asname:
                self.module_imports[alias.asname] = alias.name
            else:
                # `import a.b.c` binds `a`; attribute chains rebuild the rest.
                head = alias.name.split(".")[0]
                self.module_imports[head] = head
            self._note_import_root(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        base = node.module or ""
        if node.level:
            package_parts = self._get_module_path().split(".")[:-1]
            if node.level > 1:
                package_parts = package_parts[: -(node.level - 1)] or package_parts[:1]
            prefix = ".".join(package_parts)
            base = f"{prefix}.{base}" if base else prefix
        for alias in node.names:
            if alias.name == "*":
                continue
            target = f"{base}.{alias.name}" if base else alias.name
            self.from_imports[alias.asname or alias.name] = target
        if base:
            self._note_import_root(base)
        self.generic_visit(node)

    def _note_import_root(self, target: str) -> None:
        root = target.split(".")[0]
        if not root or root in PYTHON_STDLIB_MODULES:
            return
        if self.project_modules is not None and not is_project_import(target, self.project_modules):
            self.external_import_roots.add(root)

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef):
        """Visit class definition, extract it and its method table."""
        if self._inside_function():
            # Classes local to a function body are implementation details:
            # traverse for calls, but do not extract components for them.
            self.scope_stack.append(("class", node.name))
            self.generic_visit(node)
            self.scope_stack.pop()
            return

        base_classes = [self._extract_base_class_name(base) for base in node.bases]
        base_classes = [name for name in base_classes if name is not None]

        enclosing = self._current_class_dotted()
        class_dotted = f"{enclosing}.{node.name}" if enclosing else node.name
        relative_path = self._get_relative_path()
        component_id = f"{relative_path}::{class_dotted}"

        self.class_methods[class_dotted] = {
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.class_bases[class_dotted] = base_classes

        class_node = Node(
            id=component_id,
            name=class_dotted,
            component_type="class",
            file_path=str(self.file_path),
            relative_path=relative_path,
            source_code="\n".join(self.lines[node.lineno - 1 : node.end_lineno or node.lineno]),
            start_line=node.lineno,
            end_line=node.end_lineno,
            has_docstring=bool(ast.get_docstring(node)),
            docstring=ast.get_docstring(node) or "",
            parameters=None,
            node_type="class",
            base_classes=base_classes if base_classes else None,
            class_name=enclosing,
            display_name=f"class {class_dotted}",
            component_id=component_id,
            language="python",
            qualified_name=f"{self._get_module_path()}.{class_dotted}",
        )
        self.nodes.append(class_node)
        if not enclosing:
            self.top_level_nodes[node.name] = class_node

        for base_name in base_classes:
            resolved = self._resolve_name_reference(base_name)
            if resolved:
                self.call_relationships.append(
                    CallRelationship(
                        caller=component_id,
                        callee=resolved[0],
                        call_line=node.lineno,
                        is_resolved=resolved[1],
                    )
                )

        self.scope_stack.append(("class", node.name))
        self.component_stack.append(component_id)
        self.generic_visit(node)
        self.component_stack.pop()
        self.scope_stack.pop()

    def _extract_base_class_name(self, base):
        """Extract base class name from AST node."""
        if isinstance(base, ast.Name):
            return base.id
        elif isinstance(base, ast.Attribute):
            parts = []
            node = base
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    def _process_function_node(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        """Process a function definition: top-level functions and class
        methods become components; functions nested inside functions only
        contribute calls to their enclosing component."""
        relative_path = self._get_relative_path()
        is_method = (
            bool(self.scope_stack)
            and self.scope_stack[-1][0] == "class"
            and self._current_class_dotted() in self.class_methods
        )
        is_top_level = not self.scope_stack

        func_node = None
        if is_top_level:
            component_id = f"{relative_path}::{node.name}"
            func_node = Node(
                id=component_id,
                name=node.name,
                component_type="function",
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code="\n".join(self.lines[node.lineno - 1 : node.end_lineno or node.lineno]),
                start_line=node.lineno,
                end_line=node.end_lineno,
                has_docstring=bool(ast.get_docstring(node)),
                docstring=ast.get_docstring(node) or "",
                parameters=[arg.arg for arg in node.args.args],
                node_type="function",
                base_classes=None,
                class_name=None,
                display_name=f"function {node.name}",
                component_id=component_id,
                language="python",
                qualified_name=f"{self._get_module_path()}.{node.name}",
            )
            if self._should_include_function(func_node):
                self.nodes.append(func_node)
                self.top_level_nodes[node.name] = func_node
            else:
                func_node = None
        elif is_method:
            class_dotted = self._current_class_dotted()
            method_dotted = f"{class_dotted}.{node.name}"
            component_id = f"{relative_path}::{method_dotted}"
            func_node = Node(
                id=component_id,
                name=method_dotted,
                component_type="method",
                file_path=str(self.file_path),
                relative_path=relative_path,
                source_code="\n".join(self.lines[node.lineno - 1 : node.end_lineno or node.lineno]),
                start_line=node.lineno,
                end_line=node.end_lineno,
                has_docstring=bool(ast.get_docstring(node)),
                docstring=ast.get_docstring(node) or "",
                parameters=[arg.arg for arg in node.args.args],
                node_type="method",
                base_classes=None,
                class_name=class_dotted,
                display_name=f"method {method_dotted}",
                component_id=component_id,
                language="python",
                qualified_name=f"{self._get_module_path()}.{method_dotted}",
            )
            if self._should_include_function(func_node):
                self.nodes.append(func_node)
            else:
                func_node = None

        if not func_node and self.local_function_names:
            self.local_function_names[-1].add(node.name)

        self.scope_stack.append(("function", node.name))
        self.local_function_names.append(set())
        if func_node:
            self.component_stack.append(func_node.id)
        self.generic_visit(node)
        if func_node:
            self.component_stack.pop()
        self.local_function_names.pop()
        self.scope_stack.pop()

    def _should_include_function(self, func: Node) -> bool:
        bare_name = func.name.split(".")[-1]
        if bare_name.startswith("_test_"):
            return False
        return True

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Visit function definition and extract function information."""
        self._process_function_node(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Visit async function definition and extract function information."""
        self._process_function_node(node)

    # ------------------------------------------------------------------
    # Assignments (shallow receiver knowledge)
    # ------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign):
        self._track_assignment(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is not None:
            self._track_assignment([node.target], node.value)
        self.generic_visit(node)

    def _track_assignment(self, targets, value) -> None:
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return
        name = targets[0].id
        if not isinstance(value, ast.Call):
            return
        chain = self._attribute_chain(value.func)
        if not chain:
            return
        root, rest = chain[0], chain[1:]
        if root in self.top_level_nodes and not rest:
            if self.top_level_nodes[root].component_type == "class":
                self.var_types[name] = root
        elif root in self.from_imports:
            self.var_origins[name] = ".".join([self.from_imports[root], *rest])
        elif root in self.module_imports:
            self.var_origins[name] = ".".join([self.module_imports[root], *rest])

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call):
        """Visit a call and emit at most one relationship for it."""
        if self._caller_id():
            classified = self._classify_call(node.func)
            if classified:
                callee, is_resolved = classified
                self._add_relationship(callee, is_resolved, node.lineno)
        self.generic_visit(node)

    def _classify_call(self, func) -> Optional[Tuple[str, bool]]:
        """Map a call target to (callee, is_resolved).

        Bare names check same-file definitions, then imports. Attribute calls
        are resolved receiver-first: self/cls/super against the enclosing
        class (with same-file inheritance), known classes and typed variables
        against their method tables, imported modules to canonical dotted
        names. Built-in filtering happens after global resolution, never here.
        """
        if isinstance(func, ast.Name):
            return self._resolve_name_reference(func.id)

        if not isinstance(func, ast.Attribute):
            return None

        chain = self._attribute_chain(func)
        if chain is None:
            # Composite receiver (call result, subscript, literal): only the
            # method name is knowable. A core-object method on an unknowable
            # receiver could never resolve to a project component; anything
            # else is kept as an honest unresolved bare name.
            if func.attr in PYTHON_OBJECT_METHODS:
                return None
            return (func.attr, False)

        root, rest = chain[0], chain[1:]

        if root in ("self", "cls") and len(rest) == 1:
            return self._resolve_method_on_class(self._current_class_dotted(), rest[0])

        if root == "super()" and len(rest) == 1:
            class_dotted = self._current_class_dotted()
            if class_dotted:
                resolved = self._resolve_method_via_bases(class_dotted, rest[0])
                if resolved:
                    return resolved
            return (rest[0], False)

        if rest:
            method = rest[-1]
            if root in self.var_types:
                return (
                    self._resolve_method_on_class(self.var_types[root], method)
                    if len(rest) == 1
                    else (".".join([self.var_types[root], *rest]), False)
                )
            if root in self.class_methods and len(rest) == 1:
                return self._resolve_method_on_class(root, method)
            if root in self.from_imports:
                return (".".join([self.from_imports[root], *rest]), False)
            if root in self.module_imports:
                return (".".join([self.module_imports[root], *rest]), False)
            if root in self.var_origins:
                return (".".join([self.var_origins[root], *rest]), False)

        return (".".join(chain), False)

    def _resolve_name_reference(self, name: str) -> Optional[Tuple[str, bool]]:
        """Resolve a bare name reference (call target or base class)."""
        if not name:
            return None
        if any(name in local for local in self.local_function_names):
            return None
        if "." in name:
            # Dotted base-class names route through the attribute logic.
            head, *rest = name.split(".")
            if head in self.module_imports:
                return (".".join([self.module_imports[head], *rest]), False)
            if head in self.from_imports:
                return (".".join([self.from_imports[head], *rest]), False)
            return (name, False)
        if name in self.top_level_nodes:
            return (f"{self._get_relative_path()}::{name}", True)
        if name in self.from_imports:
            return (self.from_imports[name], False)
        if name in self.module_imports:
            return (self.module_imports[name], False)
        return (name, False)

    def _resolve_method_on_class(
        self, class_dotted: Optional[str], method: str
    ) -> Tuple[str, bool]:
        if class_dotted:
            if method in self.class_methods.get(class_dotted, ()):
                return (f"{self._get_relative_path()}::{class_dotted}.{method}", True)
            inherited = self._resolve_method_via_bases(class_dotted, method)
            if inherited:
                return inherited
            return (f"{self._get_module_path()}.{class_dotted}.{method}", False)
        return (method, False)

    def _resolve_method_via_bases(
        self, class_dotted: str, method: str
    ) -> Optional[Tuple[str, bool]]:
        seen = set()
        queue = list(self.class_bases.get(class_dotted, ()))
        while queue:
            base = queue.pop(0)
            if base in seen:
                continue
            seen.add(base)
            if method in self.class_methods.get(base, ()):
                return (f"{self._get_relative_path()}::{base}.{method}", True)
            if base in self.class_bases:
                queue.extend(self.class_bases[base])
                continue
            resolved_base = self._resolve_name_reference(base)
            if resolved_base and not resolved_base[1] and "." in resolved_base[0]:
                # Imported base: emit the canonical dotted method for the
                # global resolver to match against qualified names.
                return (f"{resolved_base[0]}.{method}", False)
        return None

    def _attribute_chain(self, node) -> Optional[List[str]]:
        """Flatten an attribute chain to its dotted parts.

        Returns None when the receiver is not a simple name chain (a call
        result, subscript, literal, ...), except `super()` which is folded
        into a "super()" root so override calls can resolve.
        """
        parts: List[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
        ):
            parts.append("super()")
        else:
            return None
        return list(reversed(parts))

    def analyze(self):
        """Analyze the Python file and extract functions and relationships."""

        try:
            # Suppress SyntaxWarnings about invalid escape sequences in source code
            # These warnings come from regex patterns like '\(' or '\.' in the analyzed files
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=SyntaxWarning)
                tree = ast.parse(self.content)
            self.visit(tree)

            logger.debug(
                f"Python analysis complete for {self.file_path}: {len(self.nodes)} nodes, "
                f"{len(self.call_relationships)} relationships"
            )
        except SyntaxError as e:
            logger.warning(f"Could not parse {self.file_path}: {e}")
        except Exception as e:
            logger.error(f"Error analyzing {self.file_path}: {e}", exc_info=True)


def analyze_python_file(
    file_path: str,
    content: str,
    repo_path: Optional[str] = None,
    project_modules: Optional[Set[str]] = None,
) -> Tuple[List[Node], List[CallRelationship], Set[str]]:
    """
    Analyze a Python file and return components, relationships, and the
    third-party import roots observed in the file.

    Args:
        file_path: Path to the Python file
        content: Content of the Python file
        repo_path: Repository root path for calculating relative paths
        project_modules: Dotted module paths of all Python files in the repo

    Returns:
        tuple: (nodes, call_relationships, external_import_roots)
    """

    analyzer = PythonASTAnalyzer(file_path, content, repo_path, project_modules)
    analyzer.analyze()
    return analyzer.nodes, analyzer.call_relationships, analyzer.external_import_roots
