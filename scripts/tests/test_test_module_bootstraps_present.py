"""Guard 3: every test module importing a repo module by bare name carries its own bootstrap.

This is the test-module half of `test_script_bootstraps_present.py`, which skips exactly the
files this one reads (`test_*.py` and `conftest.py`). The two are jointly exhaustive over the
repo's Python.

pytest puts a test module's OWN directory on `sys.path` and nothing else, plus the
`pythonpath` entries in `pyproject.toml`. A test module importing anything else by bare name
resolves only because some other module — collected earlier in the same session — already
inserted that directory. `scripts/diagnostics/tests/test_ui_login.py` imported `ui_login`
that way for as long as `test_probe_vip_placement.py` (which carries the insert) happened to
be collected first; sharding the suite (#1270, PR #1328) broke the accident and turned it
into `ModuleNotFoundError: No module named 'ui_login'` on one leg. Issue #1333.

The rule: for a bare-name import in a test module, the name must be importable from the
module's own directory or from a `pythonpath` entry. If it is not, and the name resolves to a
`.py` file somewhere else in the repo, the module must carry a `sys.path.insert` that
resolves to a directory actually providing that name.

A sibling's insert never counts — `_sys_path_insert_calls` reads one module's AST, so credit
cannot leak across files in the same directory. That is the whole point of the guard, and
`test_a_sibling_module_s_insert_does_not_count` pins it.
"""

import ast
import sys
import tomllib
from pathlib import Path

from _renovate import _tracked_files
from test_script_bootstraps_present import (
    _insert_target,
    _parsed,
    _scoped_nodes,
    _sys_path_insert_calls,
    build_import_index,
)

REPO = Path(__file__).resolve().parents[2]

_PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
_INI = _PYPROJECT["tool"]["pytest"]["ini_options"]
PYTHONPATH_DIRS = [(REPO / p).resolve() for p in _INI["pythonpath"]]
TESTPATH_DIRS = [(REPO / p).resolve() for p in _INI["testpaths"]]

# Vendored third-party Python, whose import hygiene is not ours to judge.
_EXCLUDED = ("ansible/collections",)


def _tracked_python() -> list[Path]:
    """Every tracked `.py` file, as an absolute path.

    `git ls-files` rather than `REPO.rglob` — a root-anchored rglob walks other sessions'
    `.claude/worktrees/<name>/` checkouts and judges this commit against their older copies
    (ENFORCED by `ansible/tests/repo/test_no_root_anchored_rglob.py`). This checkout is itself
    one of those worktrees, so the hazard is not hypothetical here.
    """
    return [
        REPO / rel
        for rel in _tracked_files()
        if rel.endswith(".py")
        and not any(rel == p or rel.startswith(p + "/") for p in _EXCLUDED)
    ]


def _provides(directory: Path, name: str) -> bool:
    """Whether `import <name>` succeeds with `directory` on `sys.path`.

    A flat `<name>.py`, or a `<name>/` subdirectory holding `.py` files — the repo's
    subdirectories have no `__init__.py` on purpose, so they resolve as PEP 420 namespace
    packages.
    """
    if (directory / f"{name}.py").is_file():
        return True
    sub = directory / name
    return sub.is_dir() and any(sub.glob("*.py"))


def repo_module_dirs() -> dict[str, set[Path]]:
    """Map every top-level name the repo's own Python offers to the directories offering it.

    Used only to tell "this bare name is one of ours, imported unsafely" from "this is a
    third-party package pytest's environment provides". A name colliding across roles is not
    a problem here: whichever file it resolves to, an import with no bootstrap is a bug.
    """
    index: dict[str, set[Path]] = {}
    for file in _tracked_python():
        parent = file.parent.resolve()
        index.setdefault(file.stem, set()).add(parent)
        if parent != REPO:
            index.setdefault(parent.name, set()).add(parent.parent)
    return index


def collect_test_modules() -> list[Path]:
    """Every `test_*.py` and `conftest.py` pytest collects under `testpaths`."""
    found: set[Path] = set()
    for file in _tracked_python():
        if not (file.name.startswith("test_") or file.name == "conftest.py"):
            continue
        if any(base == file.parent or base in file.parents for base in TESTPATH_DIRS):
            found.add(file.resolve())
    return sorted(found)


def _imported_names(tree: ast.Module):
    """Yield (top-level name, scope, lineno) for each absolute import in the module."""
    for node, scope in _scoped_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                continue
            yield node.module.split(".")[0], scope, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], scope, node.lineno


def find_test_bootstrap_gaps(
    files: list[Path],
    pythonpath_dirs: list[Path],
    repo_dirs: dict[str, set[Path]],
    pythonpath_index: dict[str, set[Path]] | None = None,
) -> tuple[list[tuple[Path, int, str, set[Path]]], list[tuple[Path, int, str]]]:
    """Every unsafe bare-name import, plus every one whose insert could not be evaluated.

    Explicitly parameterised — the accept/reject pair below feeds it synthetic trees under
    `tmp_path`, which a function reading `testpaths` for itself could not be handed.
    """
    if pythonpath_index is None:
        pythonpath_index = build_import_index()
    missing: list[tuple[Path, int, str, set[Path]]] = []
    unresolvable: list[tuple[Path, int, str]] = []
    for file in files:
        tree = _parsed(file)
        own_dir = file.parent.resolve()
        inserts = _sys_path_insert_calls(tree)
        for top, scope, lineno in _imported_names(tree):
            if top in sys.stdlib_module_names:
                continue
            if _provides(own_dir, top):
                continue  # pytest puts the module's own directory on sys.path
            if any(_provides(d, top) for d in pythonpath_dirs):
                continue  # pyproject's pythonpath covers it for every collected module
            providers = {d for d in repo_dirs.get(top, set()) if _provides(d, top)}
            if not providers:
                continue  # third-party: the environment provides it, not the tree

            candidates = [
                _insert_target(call, file, pythonpath_index)
                for call, s in inserts
                if s is None or (s is scope and call.lineno < lineno)
            ]
            resolved = [t for t in candidates if t is not None]
            if any(_provides(t.resolve(), top) for t in resolved):
                continue
            if candidates and not resolved:
                unresolvable.append((file, lineno, top))
                continue
            missing.append((file, lineno, top, providers))
    return missing, unresolvable


def test_every_test_module_import_has_its_own_bootstrap():
    missing, _ = find_test_bootstrap_gaps(
        collect_test_modules(), PYTHONPATH_DIRS, repo_module_dirs()
    )
    assert not missing, (
        "test module imports a repo module by bare name with no sys.path insert of its own "
        "— it resolves only while some other module is collected first:\n"
        + "\n".join(
            f"  {f.relative_to(REPO)}:{lineno} imports {top!r}, provided by "
            f"{sorted(str(d.relative_to(REPO)) for d in dirs)}"
            for f, lineno, top, dirs in missing
        )
    )


def test_no_test_module_insert_is_unresolvable():
    """An insert this evaluator cannot understand is reported, never credited."""
    _, unresolvable = find_test_bootstrap_gaps(
        collect_test_modules(), PYTHONPATH_DIRS, repo_module_dirs()
    )
    assert not unresolvable, (
        "test module whose candidate sys.path.insert could not be evaluated (extend "
        f"eval_path_expr in test_script_bootstraps_present, or add a bootstrap): {unresolvable}"
    )


def test_the_census_contains_the_modules_this_guard_exists_for():
    """Non-vacuity: name the members, not a count.

    A census that globs for its own subject returns an empty set the moment those files move,
    and an `all(...)` over nothing passes. Four of these are the modules issue #1333 is about
    — `test_ui_login.py` is the one that broke, and three siblings carry the insert it
    borrowed. The last two pin the two other census shapes: a `scripts/tests` guard, and a
    `conftest.py` outside `scripts/`.
    """
    census = {p.relative_to(REPO).as_posix() for p in collect_test_modules()}
    required = frozenset(
        {
            "scripts/diagnostics/tests/test_ui_login.py",
            "scripts/diagnostics/tests/test_probe_vip_placement.py",
            "scripts/diagnostics/tests/test_probe_readonly_rbac.py",
            "scripts/diagnostics/tests/test_probe_longhorn_blocks.py",
            "scripts/tests/test_script_bootstraps_present.py",
            "ansible/tests/conftest.py",
        }
    )
    assert required <= census, sorted(required - census)
    assert len(census) > 200, len(census)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _fixture_tree(
    tmp_path: Path, test_body: str
) -> tuple[list[Path], dict[str, set[Path]]]:
    """A `pkg/target.py` plus a `pkg/tests/test_x.py` importing it, rooted in `tmp_path`."""
    _write(tmp_path / "pkg" / "target.py", "VALUE = 1\n")
    module = _write(tmp_path / "pkg" / "tests" / "test_x.py", test_body)
    return [module], {"target": {(tmp_path / "pkg").resolve()}}


BOOTSTRAP = (
    "import sys as _sys\n"
    "from pathlib import Path as _Path\n"
    "_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))\n"
)


def test_a_test_module_with_its_own_insert_is_clean(tmp_path):
    files, repo_dirs = _fixture_tree(tmp_path, BOOTSTRAP + "from target import VALUE\n")
    missing, unresolvable = find_test_bootstrap_gaps(files, [], repo_dirs, {})
    assert not missing and not unresolvable, (missing, unresolvable)


def test_the_os_path_bootstrap_spelling_is_clean(tmp_path):
    """The shape most test modules here actually use, and the one `eval_path_expr` had to
    grow to read: `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`."""
    files, repo_dirs = _fixture_tree(
        tmp_path,
        "import os\nimport sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from target import VALUE\n",
    )
    missing, unresolvable = find_test_bootstrap_gaps(files, [], repo_dirs, {})
    assert not missing and not unresolvable, (missing, unresolvable)


def test_a_test_module_without_an_insert_is_flagged(tmp_path):
    files, repo_dirs = _fixture_tree(tmp_path, "from target import VALUE\n")
    missing, unresolvable = find_test_bootstrap_gaps(files, [], repo_dirs, {})
    assert [(f.name, top) for f, _, top, _ in missing] == [("test_x.py", "target")]
    assert not unresolvable


def test_a_sibling_module_s_insert_does_not_count(tmp_path):
    """The exact accident of #1333: the insert lives in a neighbour, collected first."""
    files, repo_dirs = _fixture_tree(tmp_path, "from target import VALUE\n")
    _write(tmp_path / "pkg" / "tests" / "test_sibling.py", BOOTSTRAP)
    missing, _ = find_test_bootstrap_gaps(files, [], repo_dirs, {})
    assert [(f.name, top) for f, _, top, _ in missing] == [("test_x.py", "target")]


def test_a_pythonpath_entry_satisfies_the_import(tmp_path):
    files, repo_dirs = _fixture_tree(tmp_path, "from target import VALUE\n")
    missing, _ = find_test_bootstrap_gaps(
        files, [(tmp_path / "pkg").resolve()], repo_dirs, {}
    )
    assert not missing


def test_a_same_directory_import_needs_no_insert(tmp_path):
    _write(tmp_path / "pkg" / "tests" / "helper.py", "VALUE = 1\n")
    module = _write(
        tmp_path / "pkg" / "tests" / "test_x.py", "from helper import VALUE\n"
    )
    repo_dirs = {"helper": {(tmp_path / "pkg" / "tests").resolve()}}
    missing, _ = find_test_bootstrap_gaps([module], [], repo_dirs, {})
    assert not missing


def test_an_unevaluatable_insert_is_reported_not_credited(tmp_path):
    files, repo_dirs = _fixture_tree(
        tmp_path,
        "import sys\nimport os\nsys.path.insert(0, os.environ['SOMEWHERE'])\n"
        "from target import VALUE\n",
    )
    missing, unresolvable = find_test_bootstrap_gaps(files, [], repo_dirs, {})
    assert not missing
    assert [(f.name, top) for f, _, top in unresolvable] == [("test_x.py", "target")]
