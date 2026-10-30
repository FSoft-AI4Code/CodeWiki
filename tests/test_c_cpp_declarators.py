"""C and C++ functions whose name sits inside the declarators of their return type.

The analyzers looked for the function_declarator as a direct child of a function_definition. A function returning
a pointer (`const char *f()`, `T **f()`) or a C++ reference (`T &f()`) has it inside a pointer/reference declarator,
so it was left out of the graph, with every call made from inside it: 162 of lua's ~1,290 C functions, among them
lua_tolstring and lua_pushstring. And `API T f()` (an export macro before a typedef'd return type) was named T.
"""

from codewiki.src.be.dependency_analyzer.analyzers.c import analyze_c_file
from codewiki.src.be.dependency_analyzer.analyzers.cpp import analyze_cpp_file

C_SAMPLE = """\
typedef struct State State;
typedef long lua_Integer;
static int counter;
static State *current;
static char **names;

static State *find (int idx) { return current; }
static char **all_names (void) { counter++; return names; }
const char *tostring (State *L, int idx) { find(idx); return 0; }
API lua_Integer len (State *L) { return (lua_Integer)tostring(L, 1); }
int (*pick (int k)) (int) { all_names(); return 0; }
int plain (void) { return counter; }
"""

CPP_SAMPLE = """\
class Node {
public:
    Node &parent();
    const char *name() const;
};

Node &Node::parent() { return *this; }
const char *Node::name() const { parent(); return "n"; }
static Node **roots(int n) { return 0; }
API size_t count(Node *n) { roots(1); return 0; }
"""


def _names(nodes):
    return {n.name: n for n in nodes}


def test_c_functions_returning_pointers_are_in_the_graph():
    nodes, calls = analyze_c_file("/repo/lapi.c", C_SAMPLE, "/repo")
    by_name = _names(nodes)
    for fn in ("find", "all_names", "tostring", "len", "pick", "plain"):
        assert by_name[fn].component_type == "function", fn
    assert "lua_Integer" not in by_name  # `API lua_Integer len(...)` is len, not its return type
    edges = {(c.caller.split("::")[-1], c.callee) for c in calls}
    assert {("tostring", "find"), ("len", "tostring"), ("pick", "all_names")} <= edges


def test_cpp_functions_returning_pointers_or_references_are_in_the_graph():
    nodes, calls = analyze_cpp_file("/repo/tree.cpp", CPP_SAMPLE, "/repo")
    ids = {n.id for n in nodes}
    assert {
        "tree.cpp::Node.parent",
        "tree.cpp::Node.name",
        "tree.cpp::roots",
        "tree.cpp::count",
    } <= ids
    assert not any(i.endswith("::size_t") for i in ids)
    callers = {c.caller.split("::")[-1] for c in calls}
    assert {"Node.name", "count"} <= callers
