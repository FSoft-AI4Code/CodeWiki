import logging
import os

import tree_sitter_ruby
from tree_sitter import Language, Parser

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node

logger = logging.getLogger(__name__)

MAX_RECURSION_DEPTH = 100

# Kernel/Object/Enumerable methods and common metaprogramming DSL calls that
# never point at a project component. Calls whose only signal is one of these
# names are dropped instead of emitted as unresolved relationships.
RUBY_CORE_METHODS = frozenset(
    {
        # Kernel / Object
        "puts",
        "print",
        "p",
        "pp",
        "warn",
        "raise",
        "fail",
        "throw",
        "catch",
        "require",
        "require_relative",
        "load",
        "autoload",
        "loop",
        "sleep",
        "exit",
        "exit!",
        "abort",
        "at_exit",
        "rand",
        "srand",
        "format",
        "sprintf",
        "printf",
        "gets",
        "binding",
        "caller",
        "system",
        "exec",
        "spawn",
        "fork",
        "freeze",
        "frozen?",
        "dup",
        "clone",
        "tap",
        "then",
        "send",
        "public_send",
        "__send__",
        "object_id",
        "hash",
        "inspect",
        "to_s",
        "to_str",
        "to_a",
        "to_ary",
        "to_h",
        "to_i",
        "to_int",
        "to_f",
        "to_sym",
        "to_proc",
        "to_r",
        "nil?",
        "is_a?",
        "kind_of?",
        "instance_of?",
        "respond_to?",
        "equal?",
        "eql?",
        "instance_variable_get",
        "instance_variable_set",
        "instance_variables",
        "method",
        "methods",
        "class",
        "singleton_class",
        "display",
        "itself",
        "yield_self",
        "lambda",
        "proc",
        "block_given?",
        "iterator?",
        "instance_exec",
        "instance_eval",
        "class_eval",
        "module_eval",
        "define_method",
        "define_singleton_method",
        "alias_method",
        "raise_error",
        # Module / class-body DSL
        "attr_accessor",
        "attr_reader",
        "attr_writer",
        "attr",
        "private",
        "public",
        "protected",
        "module_function",
        "private_constant",
        "public_constant",
        "private_class_method",
        "public_class_method",
        "def_delegator",
        "def_delegators",
        "delegate",
        "refine",
        "using",
        # Enumerable / collection
        "each",
        "each_with_index",
        "each_with_object",
        "each_pair",
        "each_key",
        "each_value",
        "each_slice",
        "each_cons",
        "each_char",
        "each_line",
        "each_byte",
        "map",
        "map!",
        "flat_map",
        "collect",
        "collect!",
        "select",
        "select!",
        "filter",
        "filter_map",
        "reject",
        "reject!",
        "detect",
        "find",
        "find_all",
        "find_index",
        "reduce",
        "inject",
        "sum",
        "min",
        "max",
        "min_by",
        "max_by",
        "sort",
        "sort!",
        "sort_by",
        "group_by",
        "partition",
        "chunk_while",
        "slice_when",
        "zip",
        "take",
        "take_while",
        "drop",
        "drop_while",
        "first",
        "last",
        "count",
        "size",
        "length",
        "empty?",
        "any?",
        "all?",
        "none?",
        "one?",
        "include?",
        "member?",
        "index",
        "rindex",
        "push",
        "pop",
        "shift",
        "unshift",
        "append",
        "prepend_element",
        "concat",
        "insert",
        "delete",
        "delete_at",
        "delete_if",
        "clear",
        "compact",
        "compact!",
        "flatten",
        "flatten!",
        "uniq",
        "uniq!",
        "reverse",
        "reverse!",
        "join",
        "split",
        "keys",
        "values",
        "fetch",
        "store",
        "merge",
        "merge!",
        "update",
        "key?",
        "has_key?",
        "value?",
        "has_value?",
        "dig",
        "sample",
        "shuffle",
        "each_entry",
        "entries",
        "tally",
        "cycle",
        "lazy",
        "force",
        # String
        "strip",
        "strip!",
        "lstrip",
        "rstrip",
        "chomp",
        "chomp!",
        "chop",
        "chars",
        "bytes",
        "lines",
        "upcase",
        "downcase",
        "capitalize",
        "swapcase",
        "sub",
        "sub!",
        "gsub",
        "gsub!",
        "tr",
        "squeeze",
        "start_with?",
        "end_with?",
        "match",
        "match?",
        "scan",
        "slice",
        "slice!",
        "center",
        "ljust",
        "rjust",
        "encode",
        "force_encoding",
        "unpack",
        "pack",
        "hex",
        "ord",
        "chr",
        "succ",
        "next",
        "between?",
        "clamp",
        "floor",
        "ceil",
        "round",
        "abs",
        "zero?",
        "positive?",
        "negative?",
        "even?",
        "odd?",
        "times",
        "upto",
        "downto",
        "step",
        "divmod",
        "modulo",
        "pow",
        "gcd",
        "lcm",
        # Comparison / misc operators frequently parsed as plain calls
        "call",
        "yield",
        "new_ostruct_member",
        "synchronize",
        # Test DSL (specs are usually excluded, but stay quiet if they leak in)
        "describe",
        "context",
        "it",
        "expect",
        "before",
        "after",
        "let",
        "subject",
        "allow",
        "double",
        "shared_examples",
        "it_behaves_like",
    }
)

# Receivers that are Ruby/stdlib constants rather than project classes.
RUBY_BUILTIN_CONSTANTS = frozenset(
    {
        "Array",
        "Hash",
        "String",
        "Symbol",
        "Integer",
        "Float",
        "Numeric",
        "Rational",
        "Complex",
        "Range",
        "Regexp",
        "MatchData",
        "Proc",
        "Method",
        "Object",
        "BasicObject",
        "Class",
        "Module",
        "Kernel",
        "Comparable",
        "Enumerable",
        "Enumerator",
        "Struct",
        "OpenStruct",
        "Set",
        "Time",
        "Date",
        "DateTime",
        "File",
        "Dir",
        "IO",
        "StringIO",
        "Pathname",
        "Process",
        "Thread",
        "ThreadGroup",
        "Mutex",
        "Monitor",
        "Queue",
        "SizedQueue",
        "ConditionVariable",
        "Fiber",
        "Signal",
        "Marshal",
        "JSON",
        "YAML",
        "CSV",
        "URI",
        "Net",
        "Socket",
        "TCPSocket",
        "TCPServer",
        "OpenSSL",
        "Digest",
        "SecureRandom",
        "Base64",
        "Zlib",
        "Logger",
        "ENV",
        "ARGV",
        "STDIN",
        "STDOUT",
        "STDERR",
        "Math",
        "GC",
        "ObjectSpace",
        "Exception",
        "StandardError",
        "RuntimeError",
        "ArgumentError",
        "TypeError",
        "NameError",
        "NoMethodError",
        "KeyError",
        "IndexError",
        "RangeError",
        "IOError",
        "EOFError",
        "Errno",
        "SystemExit",
        "NotImplementedError",
        "FrozenError",
        "StopIteration",
        "Interrupt",
        "LoadError",
        "SyntaxError",
        "SecurityError",
        "ScriptError",
        "EncodingError",
        "FloatDomainError",
        "ZeroDivisionError",
        "LocalJumpError",
        "SystemCallError",
        "SystemStackError",
        "NilClass",
        "TrueClass",
        "FalseClass",
        "Data",
        "Random",
        "Ractor",
        "Warning",
        "Binding",
        "TracePoint",
        "Gem",
        "RbConfig",
        "FileUtils",
        "Tempfile",
        "Timeout",
        "Forwardable",
        "Singleton",
        "Observable",
        "MonitorMixin",
        "Etc",
        "Fcntl",
        "ERB",
        "OptionParser",
        "Shellwords",
        "Open3",
        "PTY",
        "Benchmark",
        "Coverage",
        "Ripper",
        "WeakRef",
        "GetText",
    }
)

MIXIN_METHODS = frozenset({"include", "extend", "prepend"})


class TreeSitterRubyAnalyzer:
    def __init__(self, file_path: str, content: str, repo_path: str | None = None):
        self.file_path = file_path
        self.content = content
        self.repo_path = repo_path or ""
        self.nodes: list[Node] = []
        self.call_relationships: list[CallRelationship] = []
        # Same-file symbol table keyed by logical name ("Foo", "Foo.bar").
        self.top_level_nodes = {}
        self.seen_relationships = set()
        self._analyze()

    def _get_relative_path(self) -> str:
        """Get relative path from repo root."""
        if self.repo_path:
            try:
                return os.path.relpath(str(self.file_path), self.repo_path)
            except ValueError:
                return str(self.file_path)
        return str(self.file_path)

    def _get_component_id(self, name: str, parent_class: str | None = None) -> str:
        rel_path = self._get_relative_path()
        if parent_class:
            return f"{rel_path}::{parent_class}.{name}"
        return f"{rel_path}::{name}"

    def _analyze(self):
        try:
            language_capsule = tree_sitter_ruby.language()
            ruby_language = Language(language_capsule)
            parser = Parser(ruby_language)
            tree = parser.parse(bytes(self.content, "utf8"))
            root = tree.root_node
            lines = self.content.splitlines()

            self._extract_nodes(root, [], lines, 0)
            self._extract_relationships(root, None, [], 0)
        except RecursionError:
            logger.error(f"Recursion limit hit parsing Ruby file {self.file_path}")
        except Exception as e:  # noqa: BLE001 — a broken file must not abort the sweep
            logger.error(f"Error parsing Ruby file {self.file_path}: {e}")

    # ------------------------------------------------------------------
    # Pass 1: components
    # ------------------------------------------------------------------

    def _extract_nodes(self, node, scope: list[str], lines, depth: int):
        if depth > MAX_RECURSION_DEPTH:
            return

        if node.type in ("class", "module"):
            name = self._constant_text(node.child_by_field_name("name"))
            if name:
                bare_name = name.split(".")[-1]
                component_type = node.type
                base_classes = None
                if node.type == "class":
                    superclass = self._superclass_name(node)
                    if superclass:
                        base_classes = [superclass]
                self._add_node(
                    node,
                    bare_name,
                    component_type,
                    lines,
                    base_classes=base_classes,
                    qualified_name=".".join(scope + [name]),
                )
                body = node.child_by_field_name("body")
                if body is not None:
                    for child in body.children:
                        self._extract_nodes(child, scope + [bare_name], lines, depth + 1)
                return

        elif node.type == "singleton_class":
            # `class << self` — its methods are singleton methods of the
            # enclosing class, so keep the current scope.
            for child in node.children:
                self._extract_nodes(child, scope, lines, depth + 1)
            return

        elif node.type == "method":
            name_node = node.child_by_field_name("name")
            method_name = name_node.text.decode() if name_node is not None else None
            if method_name:
                self._add_method_node(node, method_name, scope, lines)
            return

        elif node.type == "singleton_method":
            name_node = node.child_by_field_name("name")
            object_node = node.child_by_field_name("object")
            method_name = name_node.text.decode() if name_node is not None else None
            if method_name:
                owner_scope = scope
                if object_node is not None and object_node.type in ("constant", "scope_resolution"):
                    owner = self._constant_text(object_node)
                    if owner:
                        owner_scope = [owner.split(".")[-1]]
                self._add_method_node(node, method_name, owner_scope, lines)
            return

        for child in node.children:
            self._extract_nodes(child, scope, lines, depth + 1)

    def _add_method_node(self, node, method_name: str, scope: list[str], lines):
        if scope:
            owner = scope[-1]
            logical_name = f"{owner}.{method_name}"
            component_type = "method"
            class_name = owner
        else:
            logical_name = method_name
            component_type = "function"
            class_name = None

        parameters = self._method_parameters(node)
        # Constructors are registered for call resolution but are not
        # documentable components themselves.
        include_in_nodes = method_name != "initialize"
        self._add_node(
            node,
            logical_name,
            component_type,
            lines,
            parameters=parameters,
            class_name=class_name,
            include_in_nodes=include_in_nodes,
        )

    def _add_node(
        self,
        node,
        logical_name: str,
        component_type: str,
        lines,
        parameters: list[str] | None = None,
        base_classes: list[str] | None = None,
        class_name: str | None = None,
        qualified_name: str | None = None,
        include_in_nodes: bool = True,
    ):
        component_id = self._get_component_id(logical_name)
        relative_path = self._get_relative_path()

        docstring = ""
        comment = node.prev_sibling
        if comment is None and node.parent is not None and node.parent.type == "body_statement":
            # A comment before the first statement of a class/module body is
            # attached as a sibling of the body itself.
            comment = node.parent.prev_sibling
        if comment is not None and comment.type == "comment":
            docstring = comment.text.decode().strip()

        start_line_idx = node.start_point[0]
        end_line_idx = node.end_point[0] + 1
        code_snippet = (
            "\n".join(lines[start_line_idx:end_line_idx]) if start_line_idx < len(lines) else ""
        )

        node_obj = Node(
            id=component_id,
            name=logical_name,
            component_type=component_type,
            file_path=str(self.file_path),
            relative_path=relative_path,
            source_code=code_snippet,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            has_docstring=bool(docstring),
            docstring=docstring,
            parameters=parameters,
            node_type=component_type,
            base_classes=base_classes,
            class_name=class_name,
            display_name=f"{component_type} {logical_name}",
            component_id=component_id,
            language="ruby",
            qualified_name=qualified_name or logical_name,
        )
        if include_in_nodes:
            self.nodes.append(node_obj)
        self.top_level_nodes[logical_name] = node_obj

    def _method_parameters(self, method_node) -> list[str] | None:
        params_node = method_node.child_by_field_name("parameters")
        if params_node is None:
            return None
        params = []
        for child in params_node.children:
            if child.type in (
                "identifier",
                "optional_parameter",
                "keyword_parameter",
                "splat_parameter",
                "hash_splat_parameter",
                "block_parameter",
            ):
                params.append(child.text.decode())
        return params or None

    # ------------------------------------------------------------------
    # Pass 2: relationships
    # ------------------------------------------------------------------

    def _extract_relationships(self, node, caller: str | None, scope: list[str], depth: int):
        if depth > MAX_RECURSION_DEPTH:
            return

        if node.type in ("class", "module"):
            name = self._constant_text(node.child_by_field_name("name"))
            if name:
                bare_name = name.split(".")[-1]
                if node.type == "class":
                    superclass = self._superclass_name(node)
                    if superclass and superclass not in RUBY_BUILTIN_CONSTANTS:
                        self._add_relationship(
                            caller=self._get_component_id(bare_name),
                            callee_name=superclass,
                            call_line=node.start_point[0] + 1,
                        )
                body = node.child_by_field_name("body")
                if body is not None:
                    for child in body.children:
                        self._extract_relationships(
                            child, bare_name, scope + [bare_name], depth + 1
                        )
                return

        elif node.type in ("method", "singleton_method"):
            name_node = node.child_by_field_name("name")
            method_name = name_node.text.decode() if name_node is not None else None
            if method_name:
                owner = scope[-1] if scope else None
                if node.type == "singleton_method":
                    object_node = node.child_by_field_name("object")
                    if object_node is not None and object_node.type in (
                        "constant",
                        "scope_resolution",
                    ):
                        named_owner = self._constant_text(object_node)
                        if named_owner:
                            owner = named_owner.split(".")[-1]
                logical_name = f"{owner}.{method_name}" if owner else method_name
                for child in node.children:
                    self._extract_relationships(child, logical_name, scope, depth + 1)
                return

        elif node.type == "call":
            self._extract_call(node, caller, scope)

        for child in node.children:
            self._extract_relationships(child, caller, scope, depth + 1)

    def _extract_call(self, node, caller: str | None, scope: list[str]):
        if caller is None:
            return

        method_node = node.child_by_field_name("method")
        receiver = node.child_by_field_name("receiver")
        method_name = method_node.text.decode() if method_node is not None else None
        if not method_name:
            return

        caller_id = self._get_component_id(caller)
        call_line = node.start_point[0] + 1
        current_class = scope[-1] if scope else None

        # Mixins: `include Foo` / `extend Foo` / `prepend Foo`.
        if receiver is None and method_name in MIXIN_METHODS:
            args = node.child_by_field_name("arguments")
            if args is not None:
                for arg in args.children:
                    if arg.type in ("constant", "scope_resolution"):
                        mixin = self._constant_text(arg)
                        if mixin and mixin.split(".")[-1] not in RUBY_BUILTIN_CONSTANTS:
                            self._add_relationship(caller_id, mixin, call_line)
            return

        # `require_relative "path/to/file"` — best-effort unresolved edge on
        # the basename; plain `require` of gems is dropped with the core set.
        if receiver is None and method_name == "require_relative":
            target = self._string_argument(node)
            if target:
                self._add_relationship(caller_id, os.path.basename(target), call_line)
            return

        if receiver is None:
            # Implicit-self call: try the enclosing class's methods, then
            # same-file top-level definitions.
            if method_name in RUBY_CORE_METHODS:
                return
            self._add_relationship(caller_id, method_name, call_line, owner_class=current_class)
            return

        if receiver.type == "self":
            if method_name in RUBY_CORE_METHODS:
                return
            self._add_relationship(caller_id, method_name, call_line, owner_class=current_class)
            return

        if receiver.type in ("constant", "scope_resolution"):
            const_name = self._constant_text(receiver)
            if not const_name:
                return
            bare_const = const_name.split(".")[-1]
            if bare_const in RUBY_BUILTIN_CONSTANTS:
                return
            if method_name == "new":
                # Instantiation is an edge to the class itself.
                if bare_const in self.top_level_nodes:
                    self._add_relationship_raw(
                        caller_id, self.top_level_nodes[bare_const].id, call_line, True
                    )
                else:
                    self._add_relationship_raw(caller_id, const_name, call_line, False)
                return
            logical = f"{bare_const}.{method_name}"
            if logical in self.top_level_nodes:
                self._add_relationship_raw(
                    caller_id, self.top_level_nodes[logical].id, call_line, True
                )
            else:
                self._add_relationship_raw(
                    caller_id, f"{const_name}.{method_name}", call_line, False
                )
            return

        if receiver.type == "identifier":
            receiver_name = receiver.text.decode()
            receiver_class = self._receiver_class(node, receiver_name)
            if receiver_class:
                logical = f"{receiver_class}.{method_name}"
                if logical in self.top_level_nodes:
                    self._add_relationship_raw(
                        caller_id, self.top_level_nodes[logical].id, call_line, True
                    )
                    return
                self._add_relationship_raw(caller_id, logical, call_line, False)
                return
            if method_name in RUBY_CORE_METHODS:
                return
            self._add_relationship_raw(
                caller_id, f"{receiver_name}.{method_name}", call_line, False
            )
            return

        # Composite receiver (a call chain, literal, ...): keep only the bare
        # method name, and only when it can't be core-library noise.
        if method_name not in RUBY_CORE_METHODS:
            self._add_relationship_raw(caller_id, method_name, call_line, False)

    def _add_relationship(
        self,
        caller: str,
        callee_name: str,
        call_line: int,
        owner_class: str | None = None,
    ):
        """Emit an edge, resolving against the same-file symbol table."""
        candidates = []
        if owner_class:
            candidates.append(f"{owner_class}.{callee_name}")
        candidates.append(callee_name)
        bare = callee_name.split(".")[-1]
        if bare != callee_name:
            candidates.append(bare)

        for candidate in candidates:
            if candidate in self.top_level_nodes:
                self._add_relationship_raw(
                    caller, self.top_level_nodes[candidate].id, call_line, True
                )
                return
        # Unresolved callees stay bare logical names — the global resolver
        # matches them against name indexes afterwards.
        self._add_relationship_raw(caller, callee_name, call_line, False)

    def _add_relationship_raw(self, caller: str, callee: str, call_line: int, resolved: bool):
        key = (caller, callee, call_line)
        if key in self.seen_relationships or caller == callee:
            return
        self.seen_relationships.add(key)
        self.call_relationships.append(
            CallRelationship(
                caller=caller,
                callee=callee,
                call_line=call_line,
                is_resolved=resolved,
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _constant_text(self, node) -> str | None:
        """Flatten a constant / Foo::Bar scope_resolution to dotted text."""
        if node is None:
            return None
        if node.type == "constant":
            return node.text.decode()
        if node.type == "scope_resolution":
            return node.text.decode().replace("::", ".").lstrip(".")
        return None

    def _superclass_name(self, class_node) -> str | None:
        superclass_node = class_node.child_by_field_name("superclass")
        if superclass_node is None:
            return None
        for child in superclass_node.children:
            name = self._constant_text(child)
            if name:
                return name.split(".")[-1]
        return None

    def _string_argument(self, call_node) -> str | None:
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return None
        for arg in args.children:
            if arg.type == "string":
                for part in arg.children:
                    if part.type == "string_content":
                        return part.text.decode()
        return None

    def _receiver_class(self, call_node, receiver_name: str) -> str | None:
        """Walk the enclosing method for `receiver_name = Const.new` to infer
        the receiver's class."""
        scope_node = call_node.parent
        while scope_node is not None and scope_node.type not in ("method", "singleton_method"):
            scope_node = scope_node.parent
        if scope_node is None:
            return None
        return self._find_new_assignment(scope_node, receiver_name, 0)

    def _find_new_assignment(self, node, variable_name: str, depth: int) -> str | None:
        if depth > MAX_RECURSION_DEPTH:
            return None
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if (
                left is not None
                and left.type == "identifier"
                and left.text.decode() == variable_name
                and right is not None
                and right.type == "call"
            ):
                receiver = right.child_by_field_name("receiver")
                method = right.child_by_field_name("method")
                if (
                    receiver is not None
                    and receiver.type in ("constant", "scope_resolution")
                    and method is not None
                    and method.text.decode() == "new"
                ):
                    const_name = self._constant_text(receiver)
                    if const_name:
                        return const_name.split(".")[-1]
        for child in node.children:
            result = self._find_new_assignment(child, variable_name, depth + 1)
            if result:
                return result
        return None


def analyze_ruby_file(
    file_path: str, content: str, repo_path: str | None = None
) -> tuple[list[Node], list[CallRelationship]]:
    analyzer = TreeSitterRubyAnalyzer(file_path, content, repo_path)
    return analyzer.nodes, analyzer.call_relationships
