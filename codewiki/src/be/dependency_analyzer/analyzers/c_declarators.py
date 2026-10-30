"""Declarator helpers shared by the C and C++ analyzers (tree-sitter-c and tree-sitter-cpp shape them alike)."""

from typing import Optional

# Declarators a return type wraps a function's name in: `char *f()`, `T **f()`, `T &f()`, `int (*f(void))(int)`.
WRAPPERS = (
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
)


def _inner(node):
    named = node.child_by_field_name("declarator")
    if named is not None:
        return named
    return next((c for c in node.named_children if c.type.endswith("declarator")), None)


def function_declarator(definition):
    """The function_declarator that names a function_definition, through the declarators its return type wraps it
    in. A function returning a function pointer has two: the innermost one holds the function's own name."""
    found = None
    node = definition.child_by_field_name("declarator")
    while node is not None:
        if node.type == "function_declarator":
            found = node
        elif node.type not in WRAPPERS:
            break
        node = _inner(node)
    return found


def macro_typed_name(declarator) -> Optional[str]:
    """`API T name(...)`: an export macro before a typedef'd return type. The parser cannot tell the macro from a
    type, so it takes T for the name and leaves the real name in an ERROR node before the parameters."""
    name = declarator.child_by_field_name("declarator")
    params = declarator.child_by_field_name("parameters")
    if name is None or name.type != "identifier":
        return None
    errors = [
        c
        for c in declarator.children
        if c.type == "ERROR"
        and c.start_byte >= name.end_byte
        and (params is None or c.end_byte <= params.start_byte)
    ]
    if len(errors) != 1 or [c.type for c in errors[0].named_children] != ["identifier"]:
        return None
    return errors[0].named_children[0].text.decode()
