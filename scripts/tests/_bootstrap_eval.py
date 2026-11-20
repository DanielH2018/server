"""How a test module's `sys.path` inserts resolve, read from its AST without running it.

`test_test_module_bootstraps_present.py` holds every test module that imports a repo module by
bare name to an insert of a directory that provides it, and refuses an insert `pythonpath`
already supplies. This module is the evaluator it reads: the index of which directory provides
each top-level name, `eval_path_expr` for the path a `sys.path.insert` argument names, and
`is_pytest_only`, the predicate for a module pytest alone runs. `_test_module_rules.py` reads
that predicate too.
"""

import ast
import tomllib
from functools import lru_cache
from pathlib import Path

from lib.repo_paths import REPO

SCRIPTS = REPO / "scripts"

_PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
PYTHONPATH_DIRS = [
    (REPO / p).resolve()
    for p in _PYPROJECT["tool"]["pytest"]["ini_options"]["pythonpath"]
]


def is_pytest_only(rel: Path) -> bool:
    """Whether pytest is the only thing that runs the module at repo-relative path `rel`.

    A `test_*.py`, a `conftest.py`, or anything under a `tests/` directory -- a fixture module a
    `test_*.py` imports has no other invoker either. Guard 3 collects what this accepts.
    """
    return (
        rel.name.startswith("test_")
        or rel.name == "conftest.py"
        or "tests" in rel.parts
    )


@lru_cache(maxsize=None)
def _parsed(file: Path) -> ast.Module:
    return ast.parse(file.read_text(), filename=str(file))


def build_import_index() -> dict[str, set[Path]]:
    """Map an importable top-level name to the directory that must be on `sys.path` for it.

    A flat module `foo.py` directly in a pythonpath dir `D` needs `D` on `sys.path` to import
    `foo`. A namespace-package subdirectory of `D` (no `__init__.py`, by design -- see
    `.claude/rules/python-layout.md`) also needs `D` on `sys.path` to import
    `<subdir>.<anything>` -- this is how `lib.docs_provenance` resolves via `scripts` (the parent of `scripts/lib`).
    """
    index: dict[str, set[Path]] = {}
    for d in PYTHONPATH_DIRS:
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir()):
            if entry.is_file() and entry.suffix == ".py":
                index.setdefault(entry.stem, set()).add(d)
            elif entry.is_dir() and not (entry / "__init__.py").exists():
                if any(entry.glob("*.py")):
                    index.setdefault(entry.name, set()).add(d)
    return index


def _resolve_module_file(dotted: str, index: dict[str, set[Path]]) -> Path | None:
    """The `.py` file a dotted absolute import name (`lib.render_guard`, `host_lib`)
    resolves to, given the same directories `sys.path` would need."""
    parts = dotted.split(".")
    dirs = index.get(parts[0])
    if not dirs:
        return None
    for base in dirs:
        candidate = base.joinpath(*parts[:-1], parts[-1] + ".py")
        if candidate.is_file():
            return candidate
    return None


def _const_int(node: ast.expr) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def eval_path_expr(
    node: ast.expr, file: Path, index: dict[str, set[Path]], seen: frozenset[Path]
) -> Path | None:
    """Best-effort evaluate a `pathlib.Path`-valued expression to an absolute path.

    `file` is the module whose `__file__` a bare `__file__` reference means -- callers
    crossing into another module's own constant (via `_resolve_name`) swap this for that
    module's path, so `Path(__file__).resolve().parents[N]` is always evaluated relative to
    the file where that expression is actually written.
    """
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in ("Path", "_Path") and len(node.args) == 1:
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id == "__file__":
                return file.resolve()
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literal = Path(arg.value)
                return literal if literal.is_absolute() else None
            return None
        if name == "str" and len(node.args) == 1:
            return eval_path_expr(node.args[0], file, index, seen)
        # The `os.path` spelling of the same arithmetic. Test modules across this repo
        # bootstrap with `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`
        # rather than `Path(__file__).resolve().parents[1]`, and guard 3
        # (test_test_module_bootstraps_present.py) reads them through this evaluator.
        if name == "dirname" and len(node.args) == 1:
            base = eval_path_expr(node.args[0], file, index, seen)
            return base.parent if base is not None else None
        if name in ("abspath", "realpath") and len(node.args) == 1:
            base = eval_path_expr(node.args[0], file, index, seen)
            return base.resolve() if base is not None else None
        if name == "join" and node.args:
            base = eval_path_expr(node.args[0], file, index, seen)
            for arg in node.args[1:]:
                if base is None:
                    return None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    base = base / arg.value
                else:
                    return None
            return base
        if name == "resolve" and isinstance(node.func, ast.Attribute) and not node.args:
            return eval_path_expr(node.func.value, file, index, seen)
        return None
    if isinstance(node, ast.Attribute):
        if node.attr == "resolve":
            return eval_path_expr(node.value, file, index, seen)
        if node.attr == "parent":
            base = eval_path_expr(node.value, file, index, seen)
            return base.parent if base is not None else None
        return None
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "parents":
            base = eval_path_expr(value.value, file, index, seen)
            n = _const_int(node.slice)
            if base is None or n is None or n < 0:
                return None
            for _ in range(n + 1):
                base = base.parent
            return base
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = eval_path_expr(node.left, file, index, seen)
        if left is None:
            return None
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return left / node.right.value
        right = eval_path_expr(node.right, file, index, seen)
        return left / right if right is not None else None
    if isinstance(node, ast.Name):
        return _resolve_name(node.id, file, index, seen)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        literal = Path(node.value)
        return literal if literal.is_absolute() else None
    return None


def _resolve_name(
    name: str, file: Path, index: dict[str, set[Path]], seen: frozenset[Path]
) -> Path | None:
    if name == "__file__":
        return file.resolve()
    tree = _parsed(file)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value = eval_path_expr(node.value, file, index, seen)
                    if value is not None:
                        return value
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound != name:
                    continue
                mod_file = _resolve_module_file(node.module, index)
                if mod_file is None or mod_file in seen:
                    continue
                return _resolve_name(alias.name, mod_file, index, seen | {mod_file})
    return None


def _scoped_nodes(tree: ast.Module):
    """Yield (node, scope) for every Import/ImportFrom/Call, where scope is `None` at module
    level or the enclosing `FunctionDef`/`AsyncFunctionDef` node for a deferred one."""

    def walk(node: ast.AST, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child)
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                yield child, scope
            yield from walk(child, scope)

    yield from walk(tree, None)


def _sys_path_insert_calls(tree: ast.Module) -> list[tuple[ast.Call, object]]:
    sys_aliases = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sys":
                    sys_aliases.add(alias.asname or "sys")
    calls = []
    for node, scope in _scoped_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("insert", "append")):
            continue
        base = func.value
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "path"
            and isinstance(base.value, ast.Name)
            and base.value.id in sys_aliases
        ):
            calls.append((node, scope))
    return calls


def _insert_target(node: ast.Call, file: Path, index) -> Path | None:
    # sys.path.insert(0, X) -> X is args[1]; sys.path.append(X) -> X is args[0].
    arg = node.args[-1]
    return eval_path_expr(arg, file, index, frozenset())
