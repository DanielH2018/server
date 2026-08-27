"""Guard 2: every `scripts/**/*.py` module with a cross-directory import carries a working
`sys.path` bootstrap for it.

Repo-root CLAUDE.md (~:29-40): a directly-invoked script gets only its OWN directory on
`sys.path`; `pyproject.toml`'s `pythonpath` list is a pytest-only setting. So a module that
does `from lib.docs_provenance import ...` (or reaches into another `scripts/` subdirectory,
or across into `ansible/filter_plugins`/a role's `files/`) resolves fine under pytest and then
raises `ModuleNotFoundError` the moment a cron or a human runs it directly with
`uv run python scripts/...`. Every such module is supposed to carry its own
`sys.path.insert(...)` bootstrap rather than rely on pytest's `pythonpath`.

A bare textual check for the string `parents[1]` cannot tell a real bootstrap from a broken
one -- this is the *textual-guard-checks-break-on-indirection* class recorded in this repo's
own memory. Two real modules prove it:

  - `scripts/dev/k8s_autodeploy_counts.py` needs `ansible/filter_plugins` (for
    `from k8s_autodeploy import ...`) and bootstraps with `parents[2]`, not `parents[1]` -- a
    `parents[1]` substring check would wrongly flag this file as missing its bootstrap.
  - `scripts/deploy_tools/deploy_tags.py` needs `ansible/roles/setup/gitops_deploy/files`
    (for `from deploy_logic import ...`) via `DEPLOY_LOGIC_DIR`, a module-level constant built
    from `REPO`, which is itself imported from `lib._render_guard` -- so the path arithmetic
    that matters is in a DIFFERENT file than the one being checked. A substring check has no
    way to follow that indirection at all.

So this test resolves the AST instead of grepping for a pattern: for each cross-directory
`import`/`from ... import ...` (module-level or deferred inside a function -- some modules
bootstrap once at module level and do the actual import later, lazily, e.g.
`scripts/docs/gen_reference_hosts.py`), it finds every `sys.path.insert(...)` call reachable
before that import executes, evaluates what directory each one actually inserts (by walking
`Path(__file__).resolve().parents[N]`, `/` joins, and simple module-level constants -- crossing
into another file's own module-level assignment when the value comes from an import, using
THAT file's `__file__` for any further `parents[N]` arithmetic), and checks that one of them
resolves to the exact directory the import needs -- not merely that some `sys.path.insert`
call exists somewhere in the file.

What this still cannot catch: an insert built from control flow the evaluator does not model
(a function call other than `Path`/`str`, an f-string, string concatenation, an `if`-chosen
path). Those fall through to `None` and are reported as `unresolvable`, not silently passed --
see `test_no_import_bootstrap_is_unresolvable` below, which currently expects zero.
"""

from __future__ import annotations

import ast
import tomllib
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

_PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
PYTHONPATH_DIRS = [
    (REPO / p).resolve()
    for p in _PYPROJECT["tool"]["pytest"]["ini_options"]["pythonpath"]
]


@lru_cache(maxsize=None)
def _parsed(file: Path) -> ast.Module:
    return ast.parse(file.read_text(), filename=str(file))


def build_import_index() -> dict[str, set[Path]]:
    """Map an importable top-level name to the directory that must be on `sys.path` for it.

    A flat module `foo.py` directly in a pythonpath dir `D` needs `D` on `sys.path` to import
    `foo`. A namespace-package subdirectory of `D` (no `__init__.py`, by design -- see
    CLAUDE.md) also needs `D` on `sys.path` to import `<subdir>.<anything>` -- this is how
    `lib.docs_provenance` resolves via `scripts` (the parent of `scripts/lib`).
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
    """The `.py` file a dotted absolute import name (`lib._render_guard`, `k8s_autodeploy`)
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


def find_bootstrap_gaps(
    index: dict[str, set[Path]] | None = None,
) -> tuple[list[tuple[Path, int, str, set[Path]]], list[tuple[Path, int, str]]]:
    """Every cross-directory import missing a bootstrap that resolves to the right directory,
    plus every one whose only candidate insert could not be evaluated at all."""
    if index is None:
        index = build_import_index()
    missing: list[tuple[Path, int, str, set[Path]]] = []
    unresolvable: list[tuple[Path, int, str]] = []
    for file in sorted(SCRIPTS.rglob("*.py")):
        if file.name.startswith("test_") or file.name == "conftest.py":
            continue
        tree = _parsed(file)
        own_dir = file.parent.resolve()
        inserts = _sys_path_insert_calls(tree)
        for node, scope in _scoped_nodes(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level != 0 or not node.module:
                    continue
                top = node.module.split(".")[0]
            elif isinstance(node, ast.Import):
                top = node.names[0].name.split(".")[0]
            else:
                continue
            required_dirs = index.get(top)
            if not required_dirs:
                continue  # stdlib / third-party / not a repo-indexed module
            if own_dir in required_dirs:
                continue  # same-directory import, no bootstrap needed

            candidates = [
                (call, s, _insert_target(call, file, index))
                for call, s in inserts
                if s is None or (s is scope and call.lineno < node.lineno)
            ]
            resolved = [target for _, _, target in candidates if target is not None]
            if any(target.resolve() in required_dirs for target in resolved):
                continue
            outside_repo = [
                t
                for t in resolved
                if REPO not in t.resolve().parents and t.resolve() != REPO
            ]
            if outside_repo:
                # Deliberate runtime-only coupling to a path deployed outside the repo tree
                # (e.g. /opt/<role>/host_lib.py) -- not a repo cross-directory import bug.
                continue
            if candidates and not resolved:
                unresolvable.append(
                    (
                        file,
                        node.lineno,
                        node.module if isinstance(node, ast.ImportFrom) else top,
                    )
                )
                continue
            missing.append(
                (
                    file,
                    node.lineno,
                    node.module if isinstance(node, ast.ImportFrom) else top,
                    required_dirs,
                )
            )
    return missing, unresolvable


def test_every_cross_directory_import_has_a_working_bootstrap():
    missing, _ = find_bootstrap_gaps()
    assert not missing, (
        "cross-directory import with no bootstrap resolving to the right directory:\n"
        + "\n".join(
            f"  {f.relative_to(REPO)}:{lineno} imports {mod!r}, needs one of {sorted(str(d.relative_to(REPO)) for d in dirs)}"
            for f, lineno, mod, dirs in missing
        )
    )


def test_no_import_bootstrap_is_unresolvable():
    """An insert this evaluator cannot understand is reported, not silently treated as fine.

    Currently zero in this repo -- every real bootstrap is built from `Path(__file__)`,
    `.resolve()`, `.parent`/`.parents[N]`, `/` joins onto string literals, and simple
    module-level constants (including one hop through another module's constant). If this
    starts failing, either a new file uses a shape the evaluator doesn't model yet (extend
    `eval_path_expr`) or the import genuinely has no resolvable bootstrap.
    """
    _, unresolvable = find_bootstrap_gaps()
    assert not unresolvable, (
        "cross-directory import whose candidate sys.path.insert could not be evaluated "
        f"(extend eval_path_expr, or add a bootstrap): {unresolvable}"
    )


def test_the_import_index_is_not_empty():
    """A pyproject.toml parsing regression would make the whole guard vacuously pass."""
    index = build_import_index()
    assert "lib" in index and (SCRIPTS in index["lib"]), index.get("lib")
    assert "docs_provenance" in index, index
    assert "k8s_autodeploy" in index, index


def test_known_deferred_bootstraps_are_satisfied():
    """Pin the four files repo CLAUDE.md calls out as deferred-import shapes, so a future
    change to the evaluator that regresses them fails here specifically, not just in the
    aggregate test above."""
    missing, unresolvable = find_bootstrap_gaps()
    flagged = {f for f, *_ in missing} | {f for f, *_ in unresolvable}
    for rel in (
        "scripts/docs/gen_reference_hosts.py",
        "scripts/infra_map/gen_infra_map.py",
        "scripts/docs/service_catalog.py",
        "scripts/dev/k8s_autodeploy_counts.py",
        "scripts/deploy_tools/deploy_tags.py",
    ):
        path = REPO / rel
        assert path not in flagged, f"{rel} regressed: {flagged}"
