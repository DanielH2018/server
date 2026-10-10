"""What the implementer may change in a red test file: append tests, and nothing else (#4214).

WHY. The green gate used to refuse any change to a red file. In #4213 the fix round appended
the test a reviewer had asked for, changed no red node, and the batch ended unlanded. Hashing
each red node instead would have let #4183's first fix through: it edited a pre-existing
function in the red file, which changes what a red node checks without touching the node.

THE RULE. Every top-level statement of the red version stays as it was, in order, compared by
`ast.dump`, so formatting and comments may change and the module docstring may be rewritten.
Anything new must be a test function, a helper function, a fixture or an import, and it may not
bind a name the red version binds or reads, or uses as a parameter: a second `def test_two`
replaces the red node pytest collects, a new import can shadow the code under test, and a new
fixture can override one a red node requests. A fixture may not be `autouse` or take `name=`,
and an xunit or `pytest_*` hook is refused, since each runs around every test in the module.

WHAT IT DOES NOT SEE. Code that runs at import time inside an allowed statement: a decorator
argument, a default value, an annotation, or an import of a new module with side effects. The
green gate still runs every red node, and CI runs the whole suite on the pushed head.
"""

import ast

# Functions pytest calls around the tests of a module rather than as tests.
XUNIT_HOOKS = frozenset(
    {
        "setup_module",
        "teardown_module",
        "setup_function",
        "teardown_function",
        "setup",
        "teardown",
        "setUpModule",
        "tearDownModule",
    }
)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def append_only(red_src: str, head_src: str) -> str:
    """Why `head_src` is more than `red_src` with tests appended, or "" when it is not.

    Args:
        red_src: the file as the red commit left it.
        head_src: the same file at the implementer's HEAD.
    """
    try:
        head = ast.parse(head_src)
    except SyntaxError as exc:
        return f"does not parse: {exc.msg} at line {exc.lineno}"
    red = ast.parse(red_src)
    kept = _body(red)
    matched = 0
    added = []
    for stmt in _body(head):
        if matched < len(kept) and ast.dump(stmt) == ast.dump(kept[matched]):
            matched += 1
        else:
            added.append(stmt)
    if matched < len(kept):
        return f"changes or removes `{_label(kept[matched])}`"
    used = _names(red)
    for stmt in added:
        problem = _addition_problem(stmt, used)
        if problem:
            return f"adds `{_label(stmt)}`, which {problem}"
    return ""


def _body(module: ast.Module) -> list[ast.stmt]:
    """The module's statements without a leading docstring."""
    body = module.body
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _label(stmt: ast.stmt) -> str:
    if isinstance(stmt, (*_FUNCTIONS, ast.ClassDef)):
        kind = "class" if isinstance(stmt, ast.ClassDef) else "def"
        return f"{kind} {stmt.name}"
    return f"{type(stmt).__name__.lower()} at line {stmt.lineno}"


def _names(module: ast.Module) -> set[str]:
    """Every name the module binds, reads or takes as a parameter, at any depth."""
    names = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(_bound(node))
    return names


def _bound(alias: ast.alias) -> str:
    """The name an import binds: `import a.b` binds `a`."""
    return alias.asname or alias.name.split(".")[0]


def _addition_problem(stmt: ast.stmt, used: set[str]) -> str:
    """Why a statement the red version lacks could change how a red test runs, or ""."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for alias in stmt.names:
            if alias.name == "*":
                return "is a star import"
            if _bound(alias) in used:
                return f"rebinds `{_bound(alias)}`"
        return ""
    if not isinstance(stmt, _FUNCTIONS):
        return "is not allowed: only test functions, fixtures, helpers and imports may be appended"
    if stmt.name in used:
        return f"rebinds `{stmt.name}`"
    if stmt.name in XUNIT_HOOKS or stmt.name.startswith("pytest_"):
        return "is a hook that runs around the tests"
    for decorator in stmt.decorator_list:
        problem = _decorator_problem(decorator)
        if problem:
            return problem
    return ""


def _decorator_problem(decorator: ast.expr) -> str:
    """Why a decorator on an appended function is refused; a mark or a plain fixture is not."""
    call = decorator if isinstance(decorator, ast.Call) else None
    target = ast.unparse(call.func if call else decorator)
    if target.startswith("pytest.mark."):
        return ""
    if target != "pytest.fixture":
        return f"carries the decorator `{target}`, which is neither a pytest mark nor a fixture"
    for keyword in call.keywords if call else ():
        if keyword.arg == "autouse":
            return "is an autouse fixture, which runs around every test"
        if keyword.arg == "name":
            return (
                "is a fixture with `name=`, which can override one a red test requests"
            )
    return ""
