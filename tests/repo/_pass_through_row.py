"""The no-pass-through-function census row: a function that only forwards its arguments.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import ast
from pathlib import Path

from _helpers import is_test_file
from _row_table import Census, Subject, tracked

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    named = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return named + [p.arg for p in (a.vararg, a.kwarg) if p is not None]


def _forwarded(call: ast.Call) -> list[str] | None:
    """The names `call` passes, each as a bare name, or None when it passes anything else."""
    names = []
    for arg in call.args:
        inner = arg.value if isinstance(arg, ast.Starred) else arg
        if not isinstance(inner, ast.Name):
            return None
        names.append(inner.id)
    for kw in call.keywords:
        if not isinstance(kw.value, ast.Name) or kw.arg not in (None, kw.value.id):
            return None
        names.append(kw.value.id)
    return names


def _callee(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The name `fn` only passes its arguments to, or None when its body does more."""
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]
    if fn.decorator_list or len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    if fn.args.defaults or any(d is not None for d in fn.args.kw_defaults):
        return None
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
        return None
    params = _params(fn)
    names = _forwarded(value)
    if not params or names is None or sorted(names) != sorted(params):
        return None
    return value.func.id


def _top_level_functions(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The module's functions and its classes' methods, never a closure a factory returns."""
    nodes = [*tree.body]
    nodes += [n for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body]
    return [n for n in nodes if isinstance(n, _FUNCTIONS)]


def _pass_through_offence(subject: Subject) -> list[str]:
    try:
        tree = ast.parse(subject.text)
    except SyntaxError:
        return []
    return [
        f"line {fn.lineno}: {fn.name} only passes its arguments to {callee}"
        for fn in _top_level_functions(tree)
        if (callee := _callee(fn))
    ]


def _production_modules() -> list[str]:
    return [rel for rel in tracked("*.py") if not is_test_file(Path(rel))]


NO_PASS_THROUGH_FUNCTIONS = Census(
    name="no-pass-through-function",
    reason=(
        "A function whose body only passes its own arguments to another callable adds a name "
        "and hides nothing, so a reader follows two definitions for one behaviour. Call the "
        "callee, and keep the name that convention asks for, such as monitor-bridge's "
        "`check_*` bodies. Not flagged: a wrapper a decorator registers, one that supplies a "
        "default, a call through an attribute, and a closure a factory returns. One that must "
        "stay goes in `allow` with the reason."
    ),
    files=_production_modules,
    offence=_pass_through_offence,
    red=(
        Subject("a.py", "def check(cfg, src):\n    return inner(cfg, src)\n"),
        Subject(
            "b.py",
            'def ref(tag, query):\n    """Doc."""\n    return flux(tag, query=query)\n',
        ),
        Subject(
            "c.py", "async def run(*args, **kw):\n    return await go(*args, **kw)\n"
        ),
    ),
    green=(
        Subject("a.py", "def check(cfg, src):\n    return inner(cfg, src, now=1.0)\n"),
        Subject("b.py", "def ident(value, _msg=None):\n    return value\n"),
        Subject(
            "c.py", "@register\ndef check(cfg, src):\n    return inner(cfg, src)\n"
        ),
        Subject("d.py", "def tick():\n    return clock()\n"),
        Subject("e.py", "def call(self, x):\n    return self.inner(x)\n"),
        Subject(
            "f.py", "def code(value, remedy=''):\n    return Code(value, remedy)\n"
        ),
        Subject("g.py", "def env(name):\n    return os.environ.get(name)\n"),
        Subject(
            "h.py",
            "def lazy(name):\n    def call(*a):\n        return heavy(*a)\n\n    return call\n",
        ),
    ),
    min_matches=200,
    must_find=frozenset(
        {
            "k8s/roles/monitor-bridge/files/checks/b2.py",
            "scripts/renderer/renderer_lib/task_inputs.py",
        }
    ),
)
