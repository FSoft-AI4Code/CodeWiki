"""
Call Graph Analyzer

Central orchestrator for multi-language call graph analysis.
Coordinates language-specific analyzers to build comprehensive call graphs
across different programming languages in a repository.
"""

import logging
import re
import signal
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node
from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
    CPP_STANDARD_HEADERS,
    is_external_symbol,
    is_macro_name,
)
from codewiki.src.be.dependency_analyzer.utils.patterns import CODE_EXTENSIONS
from codewiki.src.be.dependency_analyzer.utils.security import safe_open_text

logger = logging.getLogger(__name__)


class TimeoutError(Exception):
    """Raised when file parsing exceeds timeout."""


@contextmanager
def timeout(seconds):
    """Context manager for timeout on file parsing."""

    def signal_handler(signum, frame):
        raise TimeoutError(f"File parsing exceeded {seconds}s timeout")

    # Only use signal on Unix systems (not Windows)
    try:
        old_handler = signal.signal(signal.SIGALRM, signal_handler)
        signal.alarm(seconds)
        yield
    except AttributeError:
        # Windows doesn't support SIGALRM, skip timeout
        yield
    finally:
        try:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        except (AttributeError, ValueError):
            pass


class CallGraphAnalyzer:
    def __init__(self):
        """Initialize the call graph analyzer."""
        self.functions: dict[str, Node] = {}
        self.call_relationships: list[CallRelationship] = []
        self._python_project_modules: set = set()
        self._python_external_import_roots: set = set()
        logger.debug("CallGraphAnalyzer initialized.")

    def analyze_code_files(self, code_files: list[dict], base_dir: str) -> dict:
        """
        Complete analysis: Analyze all files to build complete call graph with all nodes.

        This approach:
        1. Analyzes all code files
        2. Extracts all functions and relationships
        3. Builds complete call graph
        4. Returns all nodes and relationships
        """
        logger.debug(f"Starting analysis of {len(code_files)} files")
        logger.info(f"📊 Parsing {len(code_files)} source files (this may take a few minutes)...")

        self.functions = {}
        self.call_relationships = []
        code_files = self._route_contextual_headers(code_files, base_dir)
        self._python_project_modules = self._collect_python_modules(code_files)
        self._python_external_import_roots = set()

        files_analyzed = 0
        files_failed = 0
        start_time = time.time()

        for idx, file_info in enumerate(code_files, 1):
            file_path = file_info["path"]
            try:
                # Log progress every file with elapsed time
                if idx % max(1, len(code_files) // 10) == 0 or idx <= 5:
                    elapsed = time.time() - start_time
                    rate = idx / elapsed if elapsed > 0 else 0
                    remaining = (len(code_files) - idx) / rate if rate > 0 else 0
                    logger.info(
                        f"  [{idx}/{len(code_files)}] {file_path} ({elapsed:.1f}s elapsed, ~{remaining:.1f}s remaining)"
                    )

                self._analyze_code_file(base_dir, file_info)
                files_analyzed += 1
            except Exception as e:  # noqa: BLE001 — one unparsable file must not abort the sweep
                files_failed += 1
                logger.warning(
                    f"  ⚠️  [{idx}/{len(code_files)}] Failed to analyze {file_path}: {str(e)[:100]}"
                )

        elapsed_time = time.time() - start_time
        logger.info(
            f"✓ Analysis complete: {files_analyzed}/{len(code_files)} files analyzed, "
            f"{files_failed} failed, {len(self.functions)} functions, {len(self.call_relationships)} relationships ({elapsed_time:.1f}s)"
        )

        logger.debug("Resolving call relationships")
        self._resolve_call_relationships()
        self._deduplicate_relationships()
        viz_data = self._generate_visualization_data()

        return {
            "call_graph": {
                "total_functions": len(self.functions),
                "total_calls": len(self.call_relationships),
                "languages_found": list({f.get("language") for f in code_files}),
                "files_analyzed": files_analyzed,
                "analysis_approach": "complete_unlimited",
            },
            "functions": [func.model_dump() for func in self.functions.values()],
            "relationships": [rel.model_dump() for rel in self.call_relationships],
            "visualization": viz_data,
        }

    def extract_code_files(self, file_tree: dict) -> list[dict]:
        """
        Extract code files from file tree structure.

        Filters files based on supported extensions and excludes test/config files.

        Args:
            file_tree: Nested dictionary representing file structure

        Returns:
            List of code file information dictionaries
        """
        code_files = []

        def traverse(tree):
            if tree["type"] == "file":
                ext = tree.get("extension", "").lower()
                if ext in CODE_EXTENSIONS:
                    name = tree["name"].lower()
                    if not any(skip in name for skip in []):
                        code_files.append(
                            {
                                "path": tree["path"],
                                "name": tree["name"],
                                "extension": ext,
                                "language": CODE_EXTENSIONS[ext],
                            }
                        )
            elif tree["type"] == "directory" and tree.get("children"):
                for child in tree["children"]:
                    traverse(child)

        traverse(file_tree)
        return code_files

    def _route_contextual_headers(self, code_files: list[dict], base_dir: str) -> list[dict]:
        """Route ambiguous .h headers per file.

        A header is parsed as C++ when its own content shows C++ signals, or
        when the repository is C++-only (so even a signal-free header cannot be
        C). In a mixed C/C++ repository, a plain C header stays routed as C.
        """
        cpp_extensions = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hxx", ".h++"}
        has_cpp_files = any(
            file_info.get("extension", "").lower() in cpp_extensions
            or file_info.get("language") == "cpp"
            for file_info in code_files
        )
        has_c_files = any(
            file_info.get("extension", "").lower() == ".c" for file_info in code_files
        )

        routed_files = []
        for file_info in code_files:
            routed = dict(file_info)
            if routed.get("extension", "").lower() == ".h" and (
                self._header_has_cpp_signal(base_dir, routed["path"])
                or has_cpp_files
                and not has_c_files
            ):
                routed["language"] = "cpp"
            routed_files.append(routed)
        return routed_files

    def _header_has_cpp_signal(self, base_dir: str, relative_path: str) -> bool:
        base = Path(base_dir)
        try:
            content = safe_open_text(base, base / relative_path)
        except Exception:  # noqa: BLE001 — unreadable file simply is not an entry point
            return False

        if re.search(
            r"\b(?:namespace\s+[A-Za-z_{:]|class\s+[A-Za-z_]|template\s*<"
            r"|typename\b|(?:public|private|protected)\s*:)",
            content,
        ):
            return True
        if "::" in content:
            return True
        for header in CPP_STANDARD_HEADERS:
            if f"#include <{header}>" in content:
                return True
        return False

    def _analyze_code_file(self, repo_dir: str, file_info: dict):
        """
        Analyze a single code file based on its language.

        Routes to appropriate language-specific analyzer.

        Args:
            repo_dir: Repository directory path
            file_info: File information dictionary
        """

        base = Path(repo_dir)
        file_path = base / file_info["path"]

        try:
            # Add timeout protection (30 seconds per file max)
            with timeout(30):
                content = safe_open_text(base, file_path)
                language = file_info["language"]
                if language == "python":
                    self._analyze_python_file(file_path, content, repo_dir)
                elif language == "javascript":
                    self._analyze_javascript_file(file_path, content, repo_dir)
                elif language == "typescript":
                    self._analyze_typescript_file(file_path, content, repo_dir)
                elif language == "java":
                    self._analyze_java_file(file_path, content, repo_dir)
                elif language == "kotlin":
                    self._analyze_kotlin_file(file_path, content, repo_dir)
                elif language == "csharp":
                    self._analyze_csharp_file(file_path, content, repo_dir)
                elif language == "c":
                    self._analyze_c_file(file_path, content, repo_dir)
                elif language == "cpp":
                    self._analyze_cpp_file(file_path, content, repo_dir)
                elif language == "php":
                    self._analyze_php_file(file_path, content, repo_dir)
                elif language == "ruby":
                    self._analyze_ruby_file(file_path, content, repo_dir)
                # else:
                #     logger.warning(
                #         f"Unsupported language for call graph analysis: {language} for file {file_path}"
                #     )

        except TimeoutError as e:
            logger.warning(f"⏱️  Timeout analyzing {file_path}: {e!s}")
        except Exception as e:  # noqa: BLE001 — per-file analysis is best-effort
            logger.debug(f"Error analyzing {file_path}: {e!s}")
            logger.debug(f"Traceback: {traceback.format_exc()}")

    def _analyze_python_file(self, file_path: str, content: str, base_dir: str):
        """
        Analyze Python file using Python AST analyzer.

        Args:
            file_path: Relative path to the Python file
            content: File content string
            base_dir: Repository base directory path
        """
        from codewiki.src.be.dependency_analyzer.analyzers.python import analyze_python_file

        try:
            functions, relationships, external_import_roots = analyze_python_file(
                file_path,
                content,
                repo_path=base_dir,
                project_modules=self._python_project_modules,
            )

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
            self._python_external_import_roots.update(external_import_roots)
        except Exception:
            logger.exception(f"Failed to analyze Python file {file_path}")

    @staticmethod
    def _collect_python_modules(code_files: list[dict]) -> set:
        """Dotted module paths for every Python file in the repository."""
        modules = set()
        for file_info in code_files:
            if file_info.get("language") != "python":
                continue
            path = file_info["path"]
            for ext in (".py", ".pyx"):
                if path.endswith(ext):
                    path = path[: -len(ext)]
                    break
            module = path.replace("/", ".").replace("\\", ".")
            module = module.removesuffix(".__init__")
            if module:
                modules.add(module)
        return modules

    def _analyze_javascript_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze JavaScript file using tree-sitter based AST analyzer

        Args:
            file_path: Relative path to the JavaScript file
            content: File content string
            repo_dir: Repository base directory
        """
        try:
            from codewiki.src.be.dependency_analyzer.analyzers.javascript import (
                analyze_javascript_file_treesitter,
            )

            functions, relationships = analyze_javascript_file_treesitter(
                file_path, content, repo_path=repo_dir
            )

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)

        except Exception:
            logger.exception(f"Failed to analyze JavaScript file {file_path}")

    def _analyze_typescript_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze TypeScript file using tree-sitter based AST analyzer

        Args:
            file_path: Relative path to the TypeScript file
            content: File content string
        """
        try:
            from codewiki.src.be.dependency_analyzer.analyzers.typescript import (
                analyze_typescript_file_treesitter,
            )

            functions, relationships = analyze_typescript_file_treesitter(
                file_path, content, repo_path=repo_dir
            )

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)

        except Exception:
            logger.exception(f"Failed to analyze TypeScript file {file_path}")

    def _analyze_c_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze C file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the C file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.c import analyze_c_file

        functions, relationships = analyze_c_file(file_path, content, repo_path=repo_dir)

        for func in functions:
            func_id = func.id if func.id else f"{file_path}:{func.name}"
            self.functions[func_id] = func

        self.call_relationships.extend(relationships)

    def _analyze_cpp_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze C++ file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the C++ file
            content: File content string
        """
        from codewiki.src.be.dependency_analyzer.analyzers.cpp import analyze_cpp_file

        functions, relationships = analyze_cpp_file(file_path, content, repo_path=repo_dir)

        for func in functions:
            func_id = func.id if func.id else f"{file_path}:{func.name}"
            self.functions[func_id] = func

        self.call_relationships.extend(relationships)

    def _analyze_java_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Java file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Java file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.java import analyze_java_file

        try:
            functions, relationships = analyze_java_file(file_path, content, repo_path=repo_dir)
            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Java file {file_path}")

    def _analyze_kotlin_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Kotlin file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Kotlin file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.kotlin import analyze_kotlin_file

        try:
            functions, relationships = analyze_kotlin_file(file_path, content, repo_path=repo_dir)
            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Kotlin file {file_path}")

    def _analyze_csharp_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze C# file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the C# file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.csharp import analyze_csharp_file

        try:
            functions, relationships = analyze_csharp_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze C# file {file_path}")

    def _analyze_php_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze PHP file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the PHP file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.php import analyze_php_file

        try:
            functions, relationships = analyze_php_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze PHP file {file_path}")

    def _analyze_ruby_file(self, file_path: str, content: str, repo_dir: str):
        """
        Analyze Ruby file using tree-sitter based analyzer.

        Args:
            file_path: Relative path to the Ruby file
            content: File content string
            repo_dir: Repository base directory
        """
        from codewiki.src.be.dependency_analyzer.analyzers.ruby import analyze_ruby_file

        try:
            functions, relationships = analyze_ruby_file(file_path, content, repo_path=repo_dir)

            for func in functions:
                func_id = func.id if func.id else f"{file_path}:{func.name}"
                self.functions[func_id] = func

            self.call_relationships.extend(relationships)
        except Exception:
            logger.exception(f"Failed to analyze Ruby file {file_path}")

    def _resolve_call_relationships(self):
        """
        Resolve function call relationships across all languages.

        Attempts to match function calls to actual function definitions,
        handling cross-language calls where possible.
        """
        for func_info in self.functions.values():
            if not func_info.language:
                file_ext = Path(func_info.file_path).suffix.lower()
                func_info.language = CODE_EXTENSIONS.get(file_ext)

        indexes = self._build_resolution_indexes()

        resolved_count = 0
        for relationship in self.call_relationships:
            if relationship.is_resolved and relationship.callee in self.functions:
                continue

            resolved_id = self._resolve_callee(relationship, indexes)
            if resolved_id:
                relationship.callee = resolved_id
                relationship.is_resolved = True
                resolved_count += 1

        dotted_packages = self._dotted_project_packages()
        self.call_relationships = [
            relationship
            for relationship in self.call_relationships
            if relationship.is_resolved
            or not self._is_external_callee(
                self._caller_language(relationship.caller),
                relationship.callee,
                dotted_packages,
            )
        ]

    def _dotted_project_packages(self) -> dict[str, set]:
        """Project packages/namespaces, partitioned by language. Java and C#
        share the namespace-origin rule: a dotted callee qualified to a package
        with no prefix relation to any project package came from a third-party
        import."""
        packages: dict[str, set] = defaultdict(set)
        for func_info in self.functions.values():
            if func_info.language in ("java", "csharp"):
                package = self._dotted_package_for_node(func_info)
                if package:
                    packages[func_info.language].add(package)
        return packages

    def _is_external_callee(
        self, language: str | None, callee: str, dotted_packages: dict[str, set]
    ) -> bool:
        """Classify a still-unresolved callee as external, after project
        resolution has had its chance.

        Rules are generic, not name lists: prefix/standard-library knowledge in
        is_external_symbol, the C/C++ ALL_CAPS macro convention (macros are
        never components, so such calls can never resolve), and Java package
        origin — a dotted name qualified to a package with no prefix relation
        to any project package came from a third-party import.
        """
        if is_external_symbol(language, callee):
            return True
        if language in ("c", "cpp") and is_macro_name(callee):
            return True
        if language in ("java", "csharp") and "." in callee and dotted_packages.get(language):
            package = callee.rsplit(".", 1)[0]
            if not any(
                package == project
                or package.startswith(project + ".")
                or project.startswith(package + ".")
                for project in dotted_packages[language]
            ):
                return True
        if language == "python" and "." in callee:
            return self._is_external_python_callee(callee)
        return False

    def _is_external_python_callee(self, callee: str) -> bool:
        """Classify a still-unresolved dotted Python callee.

        A callee whose dotted prefix aligns with a project module is an
        honest project gap, never external. Otherwise the import-origin rule
        (head was imported from outside the project) and the core-object
        method rule (a tail like ``append``/``endswith`` on an unknowable
        receiver can never be a project component) apply.
        """
        from codewiki.src.be.dependency_analyzer.analyzers.python import is_project_import
        from codewiki.src.be.dependency_analyzer.utils.external_symbols import (
            PYTHON_OBJECT_METHODS,
        )

        parts = callee.split(".")
        if self._python_project_modules:
            for end in range(len(parts) - 1, 0, -1):
                if is_project_import(".".join(parts[:end]), self._python_project_modules):
                    return False
        if parts[0] in self._python_external_import_roots:
            return True
        return parts[-1] in PYTHON_OBJECT_METHODS

    def _build_resolution_indexes(self) -> dict[str, dict]:
        """Build exact/simple-name lookup indexes, both globally and per
        language. Resolution prefers the caller's own language partition: a
        name that is unique within the caller's language resolves even when
        another language defines the same name, and names made ambiguous only
        by foreign-language components keep resolving as before."""

        def make() -> dict[str, dict[str, list[str]]]:
            return {"exact": defaultdict(list), "simple": defaultdict(list)}

        global_indexes = make()
        by_lang: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(make)

        def add(index: dict[str, list[str]], key: str | None, func_id: str) -> None:
            if key and func_id not in index[key]:
                index[key].append(func_id)

        for func_id, func_info in self.functions.items():
            targets = [global_indexes]
            if func_info.language:
                targets.append(by_lang[func_info.language])

            exact_keys = [
                func_id,
                func_info.component_id,
                func_info.qualified_name,
                func_info.name,
            ]

            names = {func_info.name}
            if func_info.component_id:
                names.add(func_info.component_id.split("::")[-1])
            if func_info.qualified_name:
                names.add(func_info.qualified_name.split(".")[-1])
                parts = func_info.qualified_name.split(".")
                if len(parts) >= 2:
                    names.add(".".join(parts[-2:]))

            for target in targets:
                for key in exact_keys:
                    add(target["exact"], key, func_id)
                for name in names:
                    add(target["simple"], name, func_id)
                    if name and "." in name:
                        add(target["simple"], name.split(".")[-1], func_id)

        return {
            "exact": global_indexes["exact"],
            "simple": global_indexes["simple"],
            "by_lang": dict(by_lang),
        }

    def _resolve_callee(
        self, relationship: CallRelationship, indexes: dict[str, dict]
    ) -> str | None:
        caller = self.functions.get(relationship.caller)
        caller_language = caller.language if caller else None

        lang_indexes = indexes["by_lang"].get(caller_language) if caller_language else None
        if lang_indexes:
            match = self._resolve_callee_in(
                relationship, lang_indexes["exact"], lang_indexes["simple"]
            )
            if match:
                return match

        return self._resolve_callee_in(relationship, indexes["exact"], indexes["simple"])

    def _resolve_callee_in(
        self,
        relationship: CallRelationship,
        exact: dict[str, list[str]],
        simple: dict[str, list[str]],
    ) -> str | None:
        callee_name = relationship.callee

        exact_match = self._unique_match(exact, callee_name)
        if exact_match:
            return exact_match

        if "::" in callee_name:
            suffix = callee_name.split("::")[-1]
            exact_match = self._unique_match(exact, suffix)
            if exact_match:
                return exact_match
            simple_match = self._unique_match(simple, suffix)
            if simple_match:
                return simple_match

        if "." in callee_name:
            simple_match = self._unique_match(simple, callee_name)
            if simple_match:
                return simple_match
            tail_match = self._unique_match(simple, callee_name.split(".")[-1])
            if tail_match:
                return tail_match

        caller = self.functions.get(relationship.caller)
        if caller and caller.language in ("java", "csharp") and "." not in callee_name:
            package = self._dotted_package_for_node(caller)
            if package:
                same_package_match = self._unique_match(exact, f"{package}.{callee_name}")
                if same_package_match:
                    return same_package_match

        return self._unique_match(simple, callee_name)

    def _unique_match(self, index: dict[str, list[str]], key: str) -> str | None:
        matches = index.get(key, [])
        return matches[0] if len(matches) == 1 else None

    def _dotted_package_for_node(self, node: Node) -> str:
        """The package (Java) / namespace (C#) a node lives in, derived from its
        dotted qualified name. Both languages share the same shape: a type's
        package is its qualified name minus the type, a method's is minus
        ``Type.method``."""
        qualified_name = node.qualified_name or ""
        parts = qualified_name.split(".")
        if len(parts) < 2:
            return ""
        if node.component_type == "method" and len(parts) >= 3:
            return ".".join(parts[:-2])
        return ".".join(parts[:-1])

    def _caller_language(self, caller_id: str) -> str | None:
        caller = self.functions.get(caller_id)
        if caller and caller.language:
            return caller.language
        if caller:
            return CODE_EXTENSIONS.get(Path(caller.file_path).suffix.lower())
        return None

    def _deduplicate_relationships(self):
        """
        Deduplicate call relationships based on caller-callee pairs.

        Removes duplicate relationships while preserving the first occurrence.
        This helps eliminate noise from multiple calls to the same function.
        """
        seen = set()
        unique_relationships = []

        for rel in self.call_relationships:
            key = (rel.caller, rel.callee)
            if key not in seen:
                seen.add(key)
                unique_relationships.append(rel)

        self.call_relationships = unique_relationships

    def _generate_visualization_data(self) -> dict:
        """
        Generate visualization data for graph rendering.

        Creates Cytoscape.js compatible graph data with nodes and edges.

        Returns:
            Dict: Visualization data with cytoscape elements and summary
        """
        cytoscape_elements = []

        for func_id, func_info in self.functions.items():
            node_classes = []
            if func_info.node_type == "method":
                node_classes.append("node-method")
            else:
                node_classes.append("node-function")

            file_ext = Path(func_info.file_path).suffix.lower()
            language = func_info.language or CODE_EXTENSIONS.get(file_ext, "unknown")
            if file_ext == ".py":
                node_classes.append("lang-python")
            elif file_ext == ".js":
                node_classes.append("lang-javascript")
            elif file_ext == ".ts":
                node_classes.append("lang-typescript")
            elif language == "c":
                node_classes.append("lang-c")
            elif language == "cpp" or file_ext in [
                ".cpp",
                ".cc",
                ".cxx",
                ".c++",
                ".hpp",
                ".hxx",
                ".h++",
            ]:
                node_classes.append("lang-cpp")
            elif file_ext in [".kt", ".kts"]:
                node_classes.append("lang-kotlin")
            elif file_ext in [".php", ".phtml", ".inc"]:
                node_classes.append("lang-php")
            elif file_ext == ".rb":
                node_classes.append("lang-ruby")

            cytoscape_elements.append(
                {
                    "data": {
                        "id": func_id,
                        "label": func_info.name,
                        "file": func_info.file_path,
                        "type": func_info.node_type or "function",
                        "language": language,
                    },
                    "classes": " ".join(node_classes),
                }
            )

        resolved_rels = [r for r in self.call_relationships if r.is_resolved]
        for rel in resolved_rels:
            cytoscape_elements.append(
                {
                    "data": {
                        "id": f"{rel.caller}->{rel.callee}",
                        "source": rel.caller,
                        "target": rel.callee,
                        "line": rel.call_line,
                    },
                    "classes": "edge-call",
                }
            )

        summary = {
            "total_nodes": len(self.functions),
            "total_edges": len(resolved_rels),
            "unresolved_calls": len(self.call_relationships) - len(resolved_rels),
        }

        return {
            "cytoscape": {"elements": cytoscape_elements},
            "summary": summary,
        }

    def generate_llm_format(self) -> dict:
        """Generate clean format optimized for LLM consumption."""
        return {
            "functions": [
                {
                    "name": func.name,
                    "file": Path(func.file_path).name,
                    "purpose": (func.docstring.split("\n")[0] if func.docstring else None),
                    "parameters": func.parameters,
                    "is_recursive": func.name
                    in [
                        rel.callee
                        for rel in self.call_relationships
                        if rel.caller.endswith(func.name)
                    ],
                }
                for func in self.functions.values()
            ],
            "relationships": {
                func.name: {
                    "calls": [
                        rel.callee.split(":")[-1]
                        for rel in self.call_relationships
                        if rel.caller.endswith(func.name) and rel.is_resolved
                    ],
                    "called_by": [
                        rel.caller.split(":")[-1]
                        for rel in self.call_relationships
                        if rel.callee.endswith(func.name) and rel.is_resolved
                    ],
                }
                for func in self.functions.values()
            },
        }

    def _select_most_connected_nodes(self, target_count: int):
        """
        Select the most connected nodes from the call graph.

        Args:
            target_count: The number of nodes to select
        """
        if len(self.functions) <= target_count:
            return

        if not self.call_relationships:
            logger.warning("No call relationships found - keeping all functions by name")
            func_ids = list(self.functions.keys())[:target_count]
            self.functions = {fid: func for fid, func in self.functions.items() if fid in func_ids}
            return

        graph = {}
        for rel in self.call_relationships:
            if rel.caller in self.functions and rel.caller not in graph:
                graph[rel.caller] = set()
            if rel.callee in self.functions and rel.callee not in graph:
                graph[rel.callee] = set()

            if rel.caller in graph and rel.callee in graph:
                graph[rel.caller].add(rel.callee)
                graph[rel.callee].add(rel.caller)

        degree_centrality = {}
        for func_id in self.functions:
            degree_centrality[func_id] = len(graph.get(func_id, set()))

        sorted_func_ids = sorted(degree_centrality, key=degree_centrality.get, reverse=True)

        selected_func_ids = sorted_func_ids[:target_count]

        len(self.functions)
        self.functions = {
            fid: func for fid, func in self.functions.items() if fid in selected_func_ids
        }

        len(self.call_relationships)
        self.call_relationships = [
            rel
            for rel in self.call_relationships
            if rel.caller in selected_func_ids and rel.callee in selected_func_ids
        ]
