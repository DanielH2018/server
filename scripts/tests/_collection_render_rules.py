"""The AST detector behind the rule that no test module renders while pytest collects it.

`test_census_rows_test_renders.py` runs it as a `Census` row over `pytest_only_modules()`. A
module that reads the k8s render or a host package at import, in a module-level assignment or
a `parametrize` argument, renders inside collection. Under xdist every other worker then waits
on `render_share`'s lock inside its own collection, where the render cannot overlap the fleet
render that `render_share.build_each` starts on another worker once collection ends (#8692).
"""

import ast

from _row_table import parse

# The shared-helper modules whose functions build the k8s render, a role template's render or
# a host package on first call.
RENDER_MODULES = frozenset({"_k8s_render", "_host_packages"})
# Their functions that take a doc they are given and render nothing.
_PURE = frozenset({"pod_spec", "pod_template"})


def render_imports(source: str) -> list[str]:
    """The rendering functions `source` imports from the render modules, by local name."""
    if not any(module in source for module in RENDER_MODULES):
        return []
    return [
        alias.asname or alias.name
        for node in ast.walk(parse(source))
        if isinstance(node, ast.ImportFrom) and node.module in RENDER_MODULES
        for alias in node.names
        if alias.name not in _PURE and not alias.name.isupper()
    ]


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _import_time_code(tree: ast.Module) -> list[ast.stmt | ast.expr]:
    """The nodes the interpreter runs at import: statements, decorators, class bodies."""
    code: list[ast.stmt | ast.expr] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            code += stmt.decorator_list
        elif isinstance(stmt, ast.ClassDef):
            code += stmt.decorator_list
            code += [s for s in stmt.body if not isinstance(s, ast.FunctionDef)]
        elif not isinstance(stmt, (ast.Import, ast.ImportFrom)):
            code.append(stmt)
    return code


def renders_at_collection(source: str) -> list[str]:
    """`line N: <reader>` for each import-time call that reaches a rendering function.

    A call reaches a reader directly, or through module-level functions of the same file that
    call one, however deep. A function that is only defined, cached or not, renders nothing
    until a test calls it.
    """
    readers = set(render_imports(source))
    if not readers:
        return []
    tree = parse(source)
    defs = {
        stmt.name: _called_names(stmt)
        for stmt in tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def reached(names: set[str], seen: frozenset[str] = frozenset()) -> set[str]:
        found = names & readers
        for name in (names & defs.keys()) - seen:
            found |= reached(defs[name], seen | {name})
        return found

    return [
        f"line {node.lineno}: {reader} runs at collection"
        for node in _import_time_code(tree)
        for reader in sorted(reached(_called_names(node)))
    ]
