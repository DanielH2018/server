"""Import every first-party module a hook file imports, the way the hook itself would.

Run as a script by `test_hook_imports_run_standalone.py`, never imported:

    python [-S] _hook_import_probe.py <hook file> <checkout root> [<other checkout> ...]

It loads the hook file without running its `main` (`run_name` is not `__main__`), so its
module-scope imports and `sys.path` inserts run as they do under `run-hook.sh`. Then, for each
import statement in the file, deferred ones included, it puts back the `sys.path` the module
scope left, applies the inserts the enclosing function makes before that import, and imports
the module. A hook catches its own ImportError and fails open, so an import that raised there
reads as a working hook; doing the import here is what makes a stale insert visible (#718).

An insert's argument is evaluated in the hook's globals. A local variable built from a function
parameter is evaluated too, after binding every parameter named `repo_root` to `<checkout
root>`: block-protected-bash derives its insert from the root of the payload's checkout.

Prints one JSON object: `failures` (each import that raised, with the error) and `files` (the
`__file__` of every module loaded from `<checkout root>` or from any `<other checkout>`).
"""

import ast
import importlib
import json
import os
import runpy
import sys

# Imported only from the dotfiles deploy at `~/.local/share/claude-guard`, which CI does not
# have. `_claude_guard` is the in-tree bootstrap whose whole job is that import.
OUT_OF_TREE = frozenset({"claude_guard", "_claude_guard"})
# The function parameters an insert's local variable may be built from, and the value each is
# bound to: the checkout the hook runs against.
PARAMETERS = ("repo_root",)


def _is_insert(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "path"
        and len(node.args) == 2
    )


def _imports(node):
    """Every absolute module name `node` imports, without descending into nested scopes."""
    names = []
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            names += [alias.name for alias in child.names]
        elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
            names.append(child.module)
    return names


def _evaluate(expr, namespace):
    return eval(compile(ast.Expression(expr), "<insert>", "eval"), namespace)


def _function_inserts(func, before, hook_globals, root):
    """The paths `func`'s own inserts put on `sys.path` before line `before`, in order."""
    local = dict(hook_globals)
    local.update({p: root for p in PARAMETERS})
    paths = []
    for node in sorted(ast.walk(func), key=lambda n: getattr(n, "lineno", 0)):
        if getattr(node, "lineno", before) >= before:
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    local[target.id] = _evaluate(node.value, local)
                except Exception:  # noqa: S110 - an unrelated local; only inserts must resolve
                    pass
        elif isinstance(node, ast.Call) and _is_insert(node):
            paths.append(_evaluate(node.args[1], local))
    return paths


def _under(path, roots):
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _first_party(roots):
    """The loaded modules whose file or package directory sits under `roots`."""
    found = {}
    for name, module in list(sys.modules.items()):
        where = getattr(module, "__file__", None) or next(
            iter(getattr(module, "__path__", None) or []), None
        )
        if where and _under(os.path.realpath(where), roots):
            found[name] = where
    return found


def main(hook, root, others):
    # What `python <hook>` puts first for a hook, and what a `hooklib` module is imported under.
    sys.path[0] = os.path.join(root, ".claude", "hooks")
    roots = [os.path.realpath(r) for r in (root, *others)]
    hook_globals = runpy.run_path(hook, run_name="__hook_import_probe__")
    base = list(sys.path)
    tree = ast.parse(open(hook, encoding="utf-8").read())
    scopes = [(None, name) for name in _imports_at_module_scope(tree)]
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(func):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    scopes += [(func, name) for name in _imports(child)]
    failures, files = [], {}
    for func, name in scopes:
        head = name.partition(".")[0]
        if head in sys.stdlib_module_names or head in OUT_OF_TREE:
            continue
        sys.path[:] = base
        for module in _first_party(roots):
            del sys.modules[module]
        try:
            if func is not None:
                line = _line_of(func, name)
                for path in reversed(_function_inserts(func, line, hook_globals, root)):
                    sys.path.insert(0, path)
            importlib.import_module(name)
        except BaseException as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
        files.update(_first_party(roots))
    files.update(_first_party(roots))
    return {"failures": failures, "files": files}


def _imports_at_module_scope(tree):
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        names += _imports(node)
    return names


def _line_of(func, name):
    for child in ast.walk(func):
        if isinstance(child, (ast.Import, ast.ImportFrom)) and name in _imports(child):
            return child.lineno
    return func.end_lineno or func.lineno


if __name__ == "__main__":
    print(json.dumps(main(os.path.realpath(sys.argv[1]), sys.argv[2], sys.argv[3:])))
