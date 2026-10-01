#!/usr/bin/env python3
"""Which scripts under ``scripts/`` import which, read from the source without running it.

``script_classify`` decides how a script runs; this module answers the one question
that decision needs from the import statements: which module stem a given import names.

An import credits a script only when the first directory that holds the module lies under
``scripts/``. Matching on the basename alone credited ``scripts/lib/gitops_markers.py`` with an
import that resolves to a role's ``files/``, and ``scripts/docs/reference/secrets.py`` with the
stdlib ``secrets``.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import tomllib
from collections.abc import Callable
from pathlib import Path

__all__ = ["import_graph", "is_test_file"]


def _all_py(scripts: Path) -> list[Path]:
    """Every .py under scripts/, including the tests — the import scan needs their stems."""
    return sorted(p for p in scripts.rglob("*.py") if "__pycache__" not in p.parts)


def is_test_file(path: Path) -> bool:
    """Whether pytest, not a person or a cron, is what runs `path`."""
    return path.name.startswith("test_") or path.name == "conftest.py"


def _resolve(parts: list[str], root: Path) -> str:
    """The module stem `parts` names under `root`, or "" when it names no module there.

    Walks past EVERY leading segment that is a real directory rather than assuming one:
    `diagnostics.probe_lib.core` has two, and stopping at the second reported a module twelve
    scripts import as something nobody runs. The segment has to be a real `.py` beside those
    directories for this to answer — `from diagnostics.probe_lib import core` names only
    directories in its module part, and the caller reads the aliases instead.
    """
    here = root
    for part in parts:
        if (here / part).is_dir():
            here /= part
            continue
        return part if (here / f"{part}.py").is_file() else ""
    return ""


def _path_expr(node: ast.AST, path: Path) -> Path | None:
    """The directory a `Path(__file__)`-rooted expression names, or None for any other shape.

    Covers the spellings the tree puts on `sys.path`: `str(...)` around
    `Path(__file__).resolve()`, then `.parent`, `.parents[N]` and `/ "segment"`.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "str" and len(node.args) == 1:
            return _path_expr(node.args[0], path)
        if (
            node.func.id in ("Path", "_Path")
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "__file__"
        ):
            return path
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "resolve" and not node.args:
            return _path_expr(node.func.value, path)
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _path_expr(node.value, path)
        return base.parent if base else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        base = _path_expr(node.value.value, path)
        return base.parents[node.slice.value] if base else None
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
    ):
        base = _path_expr(node.left, path)
        return base / node.right.value if base else None
    return None


def _path_inserts(tree: ast.AST, path: Path) -> list[Path]:
    """The directories `path` puts on `sys.path` itself, in source order."""
    # DECIDED: an insert whose argument is a name (`GITOPS_DEPLOY_FILES`, `HOST_LIB_FILES`,
    # `FILTER_PLUGINS`) is skipped rather than evaluated. Every such insert in the tree points
    # outside `scripts/`, or at `scripts/` itself, which `_roots` already covers. An import only
    # such an insert explains resolves outside the tree, so it credits nothing here (#3038).
    found: list[Path] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("insert", "append")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and node.args
        ):
            continue
        target = _path_expr(node.args[-1], path.resolve())
        if target is not None:
            found.append(target)
    return found


def _pytest_pythonpath(scripts: Path) -> list[Path]:
    """The directories pytest's `pythonpath` puts in front of every test, in order.

    A test imports a module from another directory by bare name, `from deploy_run import ...`
    in `scripts/lib/tests/test_exit_codes.py`, and only this list says where that resolves.
    Empty when the repo root holds no `pyproject.toml`, as a synthetic test tree does not.
    """
    pyproject = scripts.parent / "pyproject.toml"
    if not pyproject.is_file():
        return []
    ini = tomllib.loads(pyproject.read_text()).get("tool", {}).get("pytest", {})
    ini = ini.get("ini_options", ini)
    return [scripts.parent / entry for entry in ini.get("pythonpath", [])]


def _roots(path: Path, scripts: Path) -> list[Path]:
    """The directories an import in `path` can resolve against, nearest first.

    This is the runtime answer rather than a guess. A module reaching outside its own
    directory inserts an ancestor of its own on `sys.path` (`.claude/rules/python-layout.md`),
    and pytest's `pythonpath` lists `scripts/` and its subdirectories. So a sibling inside
    `scripts/dev/fanout_lib` spells the import `from fanout_lib.manifest import Batch` — a head
    naming its OWN package directory, which resolves against `scripts/dev` and against no other
    root. The roots stop at `scripts/`: looking further up would let a directory outside the
    tree manufacture an edge.
    """
    roots = [path.parent]
    while roots[-1] != scripts and scripts in roots[-1].parents:
        roots.append(roots[-1].parent)
    return roots


def _head(dotted: str, roots: list[Path], scripts: Path) -> str:
    """The module stem an absolute import names, from the first root that holds it.

    A root outside `scripts/` that holds the module means the import resolves there, so it
    names no script: `gitops_state.py` imports `gitops_markers` from the gitops_deploy role's
    `files/`, not `scripts/lib/gitops_markers.py`.
    """
    for root in roots:
        stem = _resolve(dotted.split("."), root)
        if stem:
            return stem if root == scripts or scripts in root.parents else ""
    return ""


def _package(dotted: str, roots: list[Path], scripts: Path) -> Path | None:
    """The package directory under `scripts/` a dotted module part names, if any root holds it."""
    for root in roots:
        package = root.joinpath(*dotted.split("."))
        if package.is_dir():
            return package if package == scripts or scripts in package.parents else None
    return None


def import_graph(scripts: Path, keep: Callable[[Path], bool]) -> dict[str, set[str]]:
    """Module stem -> the filenames satisfying `keep` that import it.

    `keep` decides which files count as importers; the two callers below split test files
    from the rest, because the answer to "is this a library" and the answer to "is this
    reachable at all" do not take the same importers.
    """
    stems = {p.stem for p in _all_py(scripts)}
    pythonpath = _pytest_pythonpath(scripts)
    found: dict[str, set[str]] = {}
    for path in _all_py(scripts):
        if not keep(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError, ValueError, UnicodeDecodeError:
            continue
        # `_roots` first, then what the file puts on `sys.path` itself, then, for a test,
        # pytest's `pythonpath`. An import none of them explains resolves outside `scripts/`
        # (the stdlib, a role's `files/`) and credits nothing: matching on the bare name
        # credited `scripts/docs/reference/secrets.py` with `import secrets`.
        roots = _roots(path, scripts) + _path_inserts(tree, path)
        if is_test_file(path):
            roots += pythonpath
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [_head(alias.name, roots, scripts) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import resolves against the package directory `node.level` says,
                # and against nothing else. `from .citations import Citation` in
                # `scripts/lib/facts/atoms.py` is how a package's own members reach each other,
                # and skipping those read `citations.py` as a script nobody runs.
                if node.level:
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    head = _resolve(node.module.split("."), base) if node.module else ""
                    if head:
                        names = [head]
                    else:
                        names = [
                            alias.name
                            for alias in node.names
                            if (base / f"{alias.name}.py").is_file()
                        ]
                elif not node.module:
                    names = []
                else:
                    head = _head(node.module, roots, scripts)
                    # `from diagnostics.probe_lib import core` names only directories in the
                    # module part, so the modules imported are the aliases, and only the ones
                    # that directory holds: `from ansible.plugins.filter import core` names no
                    # script, whatever else under `scripts/` is called `core.py`.
                    package = None if head else _package(node.module, roots, scripts)
                    names = (
                        [head]
                        if head
                        else [
                            alias.name
                            for alias in node.names
                            if package and (package / f"{alias.name}.py").is_file()
                        ]
                    )
            else:
                continue
            for name in names:
                if name in stems and name != path.stem:
                    found.setdefault(name, set()).add(path.name)
    return found
