import ast
import logging
from typing import List, Tuple, Optional
from pathlib import Path
import sys
import os


from ..models.core import Node, CallRelationship

logger = logging.getLogger(__name__)


class PythonASTAnalyzer(ast.NodeVisitor):

    def __init__(self, file_path: str, content: str, repo_path: Optional[str] = None):
        """
        Initialize the Python AST analyzer.

        Args:
            file_path: Path to the Python file being analyzed
            content: Raw content of the Python file
            repo_path: Repository root path for calculating relative paths
        """
        self.file_path = file_path
        self.repo_path = repo_path
        self.content = content
        self.lines = content.splitlines()
        self.nodes: List[Node] = []
        self.call_relationships: List[CallRelationship] = []
        self.current_class_name: str | None = None
        self.current_function_name: str | None = None
        
        self.top_level_nodes = {}
    
    def _get_relative_path(self) -> str:
        """Get relative path from repo root."""
        if self.repo_path:
            return os.path.relpath(self.file_path, self.repo_path)
        return str(self.file_path)

    def _get_module_path(self) -> str:
        try:
            relative_path = self._get_relative_path()
            path = relative_path
            for ext in ['.py', '.pyx']:
                if path.endswith(ext):
                    path = path[:-len(ext)]
                    break
            return path.replace('/', '.').replace('\\', '.')
        except:
            return str(self.file_path).replace('/', '.').replace('\\', '.')
    
    def _get_component_id(self, name: str) -> str:
        """Generate dot-separated component ID."""
        module_path = self._get_module_path()
        if self.current_class_name:
            return f"{module_path}.{self.current_class_name}.{name}"
        else:
            return f"{module_path}.{name}"

    def generic_visit(self, node):
        """Override generic_visit to continue AST traversal."""
        super().generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        """Visit class definition and add to top-level nodes."""

        base_classes = [self._extract_base_class_name(base) for base in node.bases]
        base_classes = [name for name in base_classes if name is not None]
        
        component_id = f"{self._get_module_path()}.{node.name}"
        relative_path = self._get_relative_path()
        
        class_node = Node(
            id=component_id,
            name=node.name,
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
            class_name=None,
            display_name=f"class {node.name}",
            component_id=component_id
        )
        self.nodes.append(class_node)
        self.top_level_nodes[node.name] = class_node

        for base_name in base_classes:
            if base_name in self.top_level_nodes:
                self.call_relationships.append(CallRelationship(
                    caller=component_id,
                    callee=f"{self._get_module_path()}.{base_name}",
                    call_line=node.lineno,
                    is_resolved=True
                ))

        self.current_class_name = node.name
        self.generic_visit(node)
        self.current_class_name = None
    
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
        """Process function definition - only add to nodes if it's top-level."""

        if not self.current_class_name:
            component_id = f"{self._get_module_path()}.{node.name}"
            relative_path = self._get_relative_path()
            
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
                component_id=component_id
            )
            if self._should_include_function(func_node):
                self.nodes.append(func_node)
                self.top_level_nodes[node.name] = func_node

        self.current_function_name = node.name
        self.generic_visit(node)
        self.current_function_name = None

    def _should_include_function(self, func: Node) -> bool:
        if func.name.startswith("_test_"):
            return False
        return True

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Visit function definition and extract function information."""
        self._process_function_node(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Visit async function definition and extract function information."""
        self._process_function_node(node)

    def visit_Call(self, node: ast.Call):
        """Visit function call nodes and record relationships between top-level nodes."""

        if self.current_class_name or (self.current_function_name and not self.current_class_name):
            call_name = self._get_call_name(node.func)
            if call_name:
                if self.current_class_name:
                    caller_id = f"{self._get_module_path()}.{self.current_class_name}"
                else:
                    caller_id = f"{self._get_module_path()}.{self.current_function_name}"
                
                if call_name in self.top_level_nodes:
                    callee_id = f"{self._get_module_path()}.{call_name}"
                else:
                    callee_id = call_name
                
                relationship = CallRelationship(
                    caller=caller_id,
                    callee=callee_id,
                    call_line=node.lineno,
                    is_resolved=call_name in self.top_level_nodes  
                )
                self.call_relationships.append(relationship)

        self.generic_visit(node)

    def _get_call_name(self, node) -> str | None:
        """
        Extract function name from a call node.
        Handles simple names, attributes (obj.method), and filters built-ins.
        """
        PYTHON_BUILTINS = {
            "print", "len", "str", "int", "float", "bool", "list", "dict", "tuple", "set",
            "range", "enumerate", "zip", "isinstance", "hasattr", "getattr", "setattr",
            "open", "super", "__import__", "type", "object", "Exception", "ValueError",
            "TypeError", "KeyError", "IndexError", "AttributeError", "ImportError",
            "max", "min", "sum", "abs", "round", "sorted", "reversed", "filter", "map",
            "any", "all", "next", "iter", "callable", "repr", "format", "exec", "eval"
        }

        if isinstance(node, ast.Name):
            if node.id in PYTHON_BUILTINS:
                return None
            return node.id
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                if node.value.id in PYTHON_BUILTINS:
                    return None
                return f"{node.value.id}.{node.attr}"
            elif isinstance(node.value, ast.Attribute):
                base_name = self._get_call_name(node.value)
                if base_name:
                    return f"{base_name}.{node.attr}"
            return node.attr
        return None

    def analyze(self):
        """Analyze the Python file and extract functions and relationships."""

        try:
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
    file_path: str, content: str, repo_path: Optional[str] = None
) -> Tuple[List[Node], List[CallRelationship]]:
    """
    Analyze a Python file and return classes, functions, methods, and relationships.

    Args:
        file_path: Path to the Python file
        content: Content of the Python file
        repo_path: Repository root path for calculating relative paths

    Returns:
        tuple: (classes, functions, methods, call_relationships)
    """

    analyzer = PythonASTAnalyzer(file_path, content, repo_path)
    analyzer.analyze()
    return analyzer.nodes, analyzer.call_relationships

