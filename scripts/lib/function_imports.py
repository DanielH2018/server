"""Whether each entry point's function-local imports execute when the module loads from the root.

`test_entry_points_answer_help.py` catches a broken top-level import, but an import inside a
subcommand never runs under `--help`: `ha_state_model refresh` can raise
`ModuleNotFoundError: probe_core` while `--help` passes.

`importlib.util.find_spec` is not enough either. It passes there, because the function-local
`diagnostics.probe_ha` itself resolved; what failed was probe_ha's own top-level
`import probe_core`. So this module EXECUTES each function-local import, which runs everything
below it, in a fresh interpreter that has only `scripts/` on `sys.path`, with the module loaded
as `<dir>.<name>`. Every real run has that root, from the venv's `.pth` or the launcher's
`PYTHONPATH` (docs/adr/0039-scripts-is-installed-in-place.md), and nothing under `scripts/`
imports a sibling by bare name, so a by-path run adds nothing an import may rely on.

The subprocess runs with `-I`, so neither the caller's `PYTHONPATH` nor pytest's `pythonpath`
can supply a directory the real invocation lacks. Each function's imports run against a
snapshot of `sys.modules` and `sys.path` taken after the module loaded, and the snapshot is
restored afterwards: one subcommand's import must not make another's pass, because a real run
only ever reaches one.

Run: uv run pytest scripts/lib/tests/script_census/test_function_imports.py
"""

import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.repo_paths import SCRIPTS
from lib.script_imports import is_test_file

__all__ = ["ENTRY_POINTS", "EXEMPTIONS", "Finding", "check"]

TIMEOUT_S = 120


# Keyed by (file, function, module), not line; stale and unused entries are reported.
EXEMPTIONS: dict[tuple[str, str, str], str] = {}


@dataclass(frozen=True)
class Finding:
    """One function-local import that failed, or one exemption that no longer applies.

    `kind` is `broken` (the import raised), `unloadable` (the module's own top level raised,
    so none of its imports could be tried), `stale-exemption` (the exempted import is no
    longer in that function) or `unused-exemption` (the exempted import no longer fails).
    """

    file: str
    function: str
    module: str
    kind: str
    detail: str = ""


def _main_guard(tree: ast.Module) -> bool:
    """Whether `tree` has a top-level `if __name__ == "__main__":`."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return True
    return False


def _py_files(scripts_root: Path) -> list[Path]:
    return sorted(
        p
        for p in scripts_root.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


def _entry_points(scripts_root: Path = SCRIPTS) -> list[Path]:
    """Every non-test module under `scripts_root` that can be run, i.e. has a `__main__` guard.

    Stated here rather than borrowed from the `--help` census: that census starts from
    `script_classify`, which labels a module `library` once something imports it, and
    `ha_state_model.py` is both imported and run.
    """
    return [
        p
        for p in _py_files(scripts_root)
        if not is_test_file(p) and _main_guard(ast.parse(p.read_text(), str(p)))
    ]


ENTRY_POINTS: list[Path] = _entry_points()


def _local_imports(tree: ast.Module) -> list[tuple[str, str, ast.stmt]]:
    """`(function, module, node)` for each import inside a function body, innermost def wins."""
    found: list[tuple[str, str, ast.stmt]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if function is not None and isinstance(child, ast.Import):
                found.extend((function, alias.name, child) for alias in child.names)
            elif function is not None and isinstance(child, ast.ImportFrom):
                found.append(
                    (function, "." * child.level + (child.module or ""), child)
                )
            visit(child, function)

    visit(tree, None)
    return found


_RUNNER = r"""
import importlib, json, sys, traceback

target, root, name, out, groups = sys.argv[1:]
groups = json.loads(groups)
results = {"unloadable": None, "broken": []}

def describe(exc):
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()

try:
    sys.path.insert(0, root)
    module = importlib.import_module(name)
except BaseException as exc:
    if isinstance(exc, KeyboardInterrupt):
        raise
    results["unloadable"] = describe(exc)
else:
    for function, imports in groups.items():
        modules, path = dict(sys.modules), list(sys.path)
        scope = dict(vars(module))
        for index, source in imports:
            try:
                exec(compile(source, target, "exec"), scope)
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                results["broken"].append([index, describe(exc)])
        sys.modules.clear()
        sys.modules.update(modules)
        sys.path[:] = path

with open(out, "w") as fh:
    json.dump(results, fh)
"""


def _run(
    path: Path, scripts_root: Path, groups: dict[str, list[tuple[int, str]]]
) -> dict:
    """Load `path` in a fresh `-I` interpreter and execute `groups`, one function at a time."""
    name = ".".join(path.relative_to(scripts_root).with_suffix("").parts)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "results.json"
        argv = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _RUNNER,
            str(path),
            str(scripts_root),
            name,
            str(out),
            json.dumps(groups),
        ]
        try:
            proc = subprocess.run(
                argv,
                cwd=scripts_root.parent,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return {"unloadable": f"timed out after {TIMEOUT_S}s", "broken": []}
        if not out.is_file():
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            return {
                "unloadable": f"exit {proc.returncode}: " + " | ".join(tail),
                "broken": [],
            }
        return json.loads(out.read_text())


def check(
    path: Path,
    *,
    scripts_root: Path = SCRIPTS,
    exemptions: dict[tuple[str, str, str], str] = EXEMPTIONS,
) -> list[Finding]:
    """Every function-local import in `path` that fails with only `scripts_root` on the path.

    Args:
        path: the module to check, loaded by its dotted name under `scripts_root`.
        scripts_root: the `scripts/` directory the module sits under.
        exemptions: `{(file, function, module): reason}`, suppressing `broken` findings.

    Returns:
        The findings, `[]` when every import executes and every exemption still applies.
    """
    scripts_root = scripts_root.resolve()
    path = path.resolve()
    rel = path.relative_to(scripts_root.parent).as_posix()
    imports = _local_imports(ast.parse(path.read_text(), str(path)))
    present = {(rel, function, module) for function, module, _ in imports}
    mine = {key for key in exemptions if key[0] == rel}
    findings = [
        Finding(*key, "stale-exemption", exemptions[key])
        for key in sorted(mine - present)
    ]
    if not imports:
        return findings

    # One entry per statement: `import a, b` is one statement and runs as one.
    groups: dict[str, list[tuple[int, str]]] = {}
    nodes: list[ast.stmt] = []
    for function, _module, node in imports:
        if node not in nodes:
            nodes.append(node)
            groups.setdefault(function, []).append((len(nodes) - 1, ast.unparse(node)))
    result = _run(path, scripts_root, groups)
    if result["unloadable"]:
        return [*findings, Finding(rel, "", "", "unloadable", result["unloadable"])]

    failed = {index: detail for index, detail in result["broken"]}
    suppress = mine & present
    used: set[tuple[str, str, str]] = set()
    for function, module, node in imports:
        key = (rel, function, module)
        index = nodes.index(node)
        if index not in failed:
            continue
        if key in suppress:
            used.add(key)
            continue
        findings.append(Finding(*key, "broken", failed[index]))
    findings += [
        Finding(*key, "unused-exemption", exemptions[key])
        for key in sorted(suppress - used)
    ]
    return findings
