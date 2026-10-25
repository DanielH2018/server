"""Whether each entry point's function-local imports execute, under each way it is loaded (#4403).

`test_entry_points_answer_help.py` catches a broken top-level import, but an import inside a
subcommand never runs under `--help`. #674 shipped that way: `ha_state_model refresh` raised
`ModuleNotFoundError: probe_core` while `--help` passed.

`importlib.util.find_spec` is not enough either. It passes #674, because the function-local
`diagnostics.probe_ha` itself resolved; what failed was probe_ha's own top-level
`import probe_core`. So this module EXECUTES each function-local import, which runs everything
below it, in a fresh interpreter that has only the `sys.path` the real load would have:

- `mode="path"`: the module's own directory, as `python scripts/<dir>/<name>.py` gives it.
- `mode="dotted"`: only `scripts/`, the module loaded as `<dir>.<name>`. This applies only to
  a module that some other module loads by dotted name (`dotted_loaded`); for any other module
  that loading never happens, so a bare sibling import there is not a bug.

The subprocess runs with `-I`, so neither the caller's `PYTHONPATH` nor pytest's `pythonpath`
can supply a directory the real invocation lacks. Each function's imports run against a
snapshot of `sys.modules` and `sys.path` taken after the module loaded, and the snapshot is
restored afterwards: one subcommand's import must not make another's pass, because a real run
only ever reaches one.

Run: uv run pytest scripts/lib/tests/test_function_imports.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lib.repo_paths import SCRIPTS
from lib.script_imports import is_test_file

__all__ = [
    "ENTRY_POINTS",
    "EXEMPTIONS",
    "OFF_TREE_IMPORTS",
    "Finding",
    "check",
    "dotted_loaded",
]

# A module that waits on a network or a lock at import is a defect the --help census already
# reports; this bound only stops one from hanging the suite.
TIMEOUT_S = 120

HSM = "scripts/home_assistant/ha_state_model.py"
VHC = "scripts/home_assistant/validate_ha_config.py"

# Function-local imports that fail under dotted-name loading and are never reached that way.
# Keyed by (file, enclosing function, imported module), not by line, so an edit above the
# import leaves the entry valid. An entry whose import has left that function is reported as
# `stale-exemption`, and one whose import no longer fails as `unused-exemption`, so the list
# cannot outlive what it excuses. The exemption applies to the dotted mode only: by path,
# every one of these imports must still execute.
EXEMPTIONS: dict[tuple[str, str, str], str] = {
    (HSM, "main", "ha_state_checks"): (
        "the `check` branch of main(); probe_lib/ha.py loads this module by dotted name for "
        "build_model() and never calls main(), so only a direct invocation reaches it"
    ),
    (VHC, "validate", "ha_state_checks"): (
        "ha_state_model.py imports only ROLE_DIR, HAConfigLoader and assemble_config by "
        "dotted name and never calls validate(), so only a direct invocation reaches it"
    ),
}

# Function-local imports of a module that lives outside the repo, from a directory the function
# itself puts on `sys.path` just before the import. No repo-side loading can resolve one, so
# these are exempt in BOTH modes, with the same stale and unused reporting as `EXEMPTIONS`.
# Empty since H7.2e, when `host_lib.py` moved into `hosts/lib/`, which the checkout can load.
OFF_TREE_IMPORTS: dict[tuple[str, str, str], str] = {}


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
    `ha_state_model.py` (#674's module) is both imported and run (#4405).
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


def _resolve_dotted(parts: list[str], base: Path) -> Path | None:
    """The `.py` under `base` that the dotted name `parts` loads, through at least one package."""
    here = base
    for i, part in enumerate(parts):
        if (here / part).is_dir() and i < len(parts) - 1:
            here /= part
            continue
        candidate = here / f"{part}.py"
        return candidate if here != base and candidate.is_file() else None
    return None


@lru_cache(maxsize=8)
def _dotted_loaded(scripts_root: Path) -> frozenset[Path]:
    loaded: set[Path] = set()
    for importer in _py_files(scripts_root):
        if is_test_file(importer):
            continue
        try:
            tree = ast.parse(importer.read_text(), str(importer))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets: list[Path | None] = []
            if isinstance(node, ast.Import):
                targets = [
                    _resolve_dotted(a.name.split("."), scripts_root) for a in node.names
                ]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    # A relative import loads its target as a submodule of the importer's
                    # package, which is a dotted-name load from wherever that package sits.
                    base = importer.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    parts = node.module.split(".") if node.module else []
                    pkg = base.joinpath(*parts)
                    targets = [pkg.with_suffix(".py")] if parts else []
                    targets += [pkg / f"{a.name}.py" for a in node.names]
                elif node.module:
                    parts = node.module.split(".")
                    targets = [_resolve_dotted(parts, scripts_root)]
                    targets += [
                        _resolve_dotted([*parts, a.name], scripts_root)
                        for a in node.names
                    ]
            for target in targets:
                if target is not None and target.is_file() and target != importer:
                    loaded.add(target.resolve())
    return frozenset(loaded)


def dotted_loaded(scripts_root: Path = SCRIPTS) -> set[Path]:
    """The modules under `scripts_root` some other non-test module loads by dotted name.

    A dotted load is one that goes through a package directory: `from home_assistant import
    ha_state_model`, `import a.b`, or a relative import. Function-local loads count. A bare
    `import sibling` does not, and neither does an import only a test makes, since a test is
    not how the module is reached in production.
    """
    return set(_dotted_loaded(scripts_root.resolve()))


# The child process: load the module the way `mode` says, then run each function's imports
# against a restored snapshot. Results go to a file, because the module's top level may print.
_RUNNER = r"""
import importlib, importlib.util, json, sys, traceback

mode, target, root, name, out, groups = sys.argv[1:]
groups = json.loads(groups)
results = {"unloadable": None, "broken": []}

def describe(exc):
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()

try:
    if mode == "path":
        sys.path.insert(0, str(__import__("pathlib").Path(target).parent))
        spec = importlib.util.spec_from_file_location("__function_imports_check__", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
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
    path: Path, mode: str, scripts_root: Path, groups: dict[str, list[tuple[int, str]]]
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
            mode,
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
    mode: str,
    scripts_root: Path = SCRIPTS,
    exemptions: dict[tuple[str, str, str], str] = EXEMPTIONS,
    off_tree: dict[tuple[str, str, str], str] = OFF_TREE_IMPORTS,
) -> list[Finding]:
    """Every function-local import in `path` that fails when the module is loaded by `mode`.

    Args:
        path: the module to check.
        mode: `"path"` for a direct invocation, `"dotted"` for a load by dotted name with only
            `scripts_root` on the path. The dotted mode reports nothing for a module no other
            module loads by dotted name, beyond stale entries in `exemptions`.
        scripts_root: the `scripts/` directory the module sits under.
        exemptions: `{(file, function, module): reason}`, suppressing `broken` findings in the
            dotted mode only.
        off_tree: the same shape, suppressing `broken` findings in both modes.

    Returns:
        The findings, `[]` when every import executes and every exemption still applies.
    """
    if mode not in ("path", "dotted"):
        raise ValueError(f"mode must be 'path' or 'dotted', not {mode!r}")
    scripts_root = scripts_root.resolve()
    path = path.resolve()
    rel = path.relative_to(scripts_root.parent).as_posix()
    imports = _local_imports(ast.parse(path.read_text(), str(path)))
    present = {(rel, function, module) for function, module, _ in imports}
    reasons = {**exemptions, **off_tree}
    mine: set[tuple[str, str, str]] = {key for key in exemptions if key[0] == rel}
    mine_off_tree: set[tuple[str, str, str]] = {
        key for key in off_tree if key[0] == rel
    }
    findings = [
        Finding(*key, "stale-exemption", reasons[key])
        for key in sorted((mine | mine_off_tree) - present)
    ]
    if not imports or (mode == "dotted" and path not in _dotted_loaded(scripts_root)):
        return findings

    # One entry per statement: `import a, b` is one statement and runs as one.
    groups: dict[str, list[tuple[int, str]]] = {}
    nodes: list[ast.stmt] = []
    for function, _module, node in imports:
        if node not in nodes:
            nodes.append(node)
            groups.setdefault(function, []).append((len(nodes) - 1, ast.unparse(node)))
    result = _run(path, mode, scripts_root, groups)
    if result["unloadable"]:
        return [*findings, Finding(rel, "", "", "unloadable", result["unloadable"])]

    failed = {index: detail for index, detail in result["broken"]}
    suppress: set[tuple[str, str, str]] = (
        (mine if mode == "dotted" else set()) | mine_off_tree
    ) & present
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
        Finding(*key, "unused-exemption", reasons[key])
        for key in sorted(suppress - used)
    ]
    return findings
