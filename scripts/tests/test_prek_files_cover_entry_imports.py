"""Each prek hook's `files` regex matches every in-repo module its Python entry imports.

CI's PR run of prek checks only the changed files, so a hook runs only when a changed path
matches its `files` regex. A module the entry imports that the regex misses can change alone
and merge green with the hook skipped; master's full sweep catches it only after the merge
(`.github/workflows/ci.yml`). `validate-k8s-manifests` missed `lib/ansible_inventory.py` and
`lib/render_context.py` this way (#4406).

The import closure is read from the source, transitively, resolving each import the way the
importing file's runtime would: against its own directory and the `scripts/` ancestors a
module there inserts, and against every directory the file puts on `sys.path` itself. An
insert of a `lib.repo_paths` constant (`ANSIBLE / "filter_plugins"`) is evaluated from that
module. A module pytest runs (a test, a conftest, a helper under `tests/`) also resolves
against every `pythonpath` root in `pyproject.toml`, which is how a bare `from _row_table
import ...` in `ansible/tests/repo/` reaches `ansible/tests/` (#5921). An import no root resolves is the stdlib or a third-party package, and needs no match.
"""

import ast
import re
import tomllib
from pathlib import Path

import pytest

from lib import repo_paths
from lib.repo_paths import REPO, SCRIPTS

_PY_TOKEN = re.compile(r"(?<![\w/.-])([\w./-]+\.py)(?![\w])")


def _directory(node: ast.AST, path: Path) -> Path | None:
    """The directory a `sys.path` insert's argument names, or None for a shape not read here."""
    match node:
        case ast.Call(func=ast.Name(id="str"), args=[arg]):
            return _directory(arg, path)
        case ast.Call(
            func=ast.Name(id="Path" | "_Path"), args=[ast.Name(id="__file__")]
        ):
            return path
        case ast.Call(func=ast.Attribute(attr="resolve", value=base), args=[]):
            return _directory(base, path)
        case ast.Attribute(attr="parent", value=base):
            found = _directory(base, path)
            return found.parent if found else None
        case ast.Subscript(
            value=ast.Attribute(attr="parents", value=base),
            slice=ast.Constant(value=int(n)),
        ):
            found = _directory(base, path)
            return found.parents[n] if found else None
        case ast.BinOp(op=ast.Div(), left=base, right=ast.Constant(value=str(segment))):
            found = _directory(base, path)
            return found / segment if found else None
        case ast.Name(id=name):
            value = getattr(repo_paths, name, None)
            return value if isinstance(value, Path) else None
    return None


# The roots pytest puts on `sys.path` for every module it collects or that a test imports.
_PYTEST_ROOTS = [
    REPO / root
    for root in tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]["pythonpath"]
]


def _runs_under_pytest(path: Path) -> bool:
    """Whether `path` is a test, a conftest or a helper under a `tests/` directory."""
    # A module outside the repo (a test's scratch file) is judged by its name alone.
    rel = path.relative_to(REPO) if REPO in path.parents else Path(path.name)
    return (
        "tests" in rel.parts[:-1]
        or rel.name.startswith("test_")
        or rel.name == "conftest.py"
    )


def _roots(path: Path, tree: ast.AST) -> list[Path]:
    roots = [path.parent]
    while SCRIPTS in roots[-1].parents:
        roots.append(roots[-1].parent)
    if _runs_under_pytest(path):
        roots += _PYTEST_ROOTS
    for node in ast.walk(tree):
        match node:
            case ast.Call(
                func=ast.Attribute(
                    attr="insert" | "append", value=ast.Attribute(attr="path")
                ),
                args=[*_, arg],
            ):
                found = _directory(arg, path)
                if found is not None:
                    roots.append(found)
    return roots


def _module(parts: list[str], root: Path) -> list[Path]:
    """The files `parts` names under `root`: a module, or a package's members named after it."""
    here = root
    for i, part in enumerate(parts):
        if (here / f"{part}.py").is_file():
            return [here / f"{part}.py"]
        if not (here / part).is_dir():
            return []
        here /= part
        if i == len(parts) - 1:
            return [here]
    return []


def imported_files(path: Path) -> set[Path]:
    """The in-repo `.py` files `path` imports directly."""
    tree = ast.parse(path.read_text())
    roots = _roots(path, tree)
    found: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            wanted = [(alias.name, []) for alias in node.names]
            bases = roots
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                bases = [base]
                wanted = [(node.module or "", names)]
            else:
                bases = roots
                wanted = [(node.module or "", names)]
        else:
            continue
        for dotted, names in wanted:
            for root in bases:
                hits = _module(dotted.split("."), root) if dotted else [root]
                if not hits:
                    continue
                for hit in hits:
                    if hit.is_dir():
                        found |= {
                            hit / f"{n}.py"
                            for n in names
                            if (hit / f"{n}.py").is_file()
                        }
                        found |= {hit / "__init__.py"} & {*hit.glob("__init__.py")}
                    else:
                        found.add(hit)
                break
    return {f.resolve() for f in found if f.resolve() != path.resolve()}


def import_closure(entry: Path) -> set[Path]:
    seen, todo = set(), [entry.resolve()]
    while todo:
        path = todo.pop()
        if path in seen or REPO not in path.parents:
            continue
        seen.add(path)
        todo += imported_files(path)
    return seen


def python_hooks(config: dict) -> list[tuple[str, Path, dict]]:
    """(id, entry module, hook) for every prek hook whose entry runs an in-repo `.py`."""
    out = []
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            for token in _PY_TOKEN.findall(hook.get("entry", "")):
                if (REPO / token).is_file():
                    out.append((hook["id"], REPO / token, hook))
    return out


def gaps(hook: dict, modules: set[Path]) -> list[str]:
    """The modules a change to which would not run `hook`."""
    if hook.get("always_run"):
        return []
    files = re.compile(hook.get("files", ""))
    exclude = re.compile(hook["exclude"]) if hook.get("exclude") else None
    rels = sorted(m.relative_to(REPO).as_posix() for m in modules)
    return [
        rel
        for rel in rels
        if not files.search(rel) or (exclude is not None and exclude.search(rel))
    ]


HOOKS = python_hooks(tomllib.loads((REPO / "prek.toml").read_text()))


def test_the_census_reads_validate_k8s_manifests_imports():
    by_id = {hook_id: entry for hook_id, entry, _ in HOOKS}
    assert "validate-k8s-manifests" in by_id, sorted(by_id)
    closure = import_closure(by_id["validate-k8s-manifests"])
    for module in ("scripts/lib/ansible_inventory.py", "scripts/lib/render_context.py"):
        assert (REPO / module).resolve() in closure, module


@pytest.mark.parametrize(
    ("hook_id", "entry", "hook"), HOOKS, ids=[hook_id for hook_id, _, _ in HOOKS]
)
def test_a_hook_runs_when_any_module_its_entry_imports_changes(hook_id, entry, hook):
    missing = gaps(hook, import_closure(entry))
    assert not missing, (
        f"prek hook {hook_id!r} imports these modules but its `files` regex does not match "
        "them, so a PR changing one alone skips the hook. Widen `files` in prek.toml:\n"
        + "\n".join(missing)
    )


def test_a_regex_matching_only_the_entry_is_flagged():
    entry = REPO / "scripts/validate/k8s_manifests.py"
    hook = {"files": r"^scripts/validate/k8s_manifests\.py$"}
    assert "scripts/lib/render_context.py" in gaps(hook, import_closure(entry))


def test_an_excluded_module_is_flagged():
    entry = REPO / "scripts/validate/k8s_manifests.py"
    hook = {"files": r"\.py$", "exclude": r"^scripts/lib/render_context\.py$"}
    assert gaps(hook, import_closure(entry)) == ["scripts/lib/render_context.py"]


def test_a_regex_matching_the_whole_closure_is_clean():
    entry = REPO / "scripts/validate/k8s_manifests.py"
    assert gaps({"files": r"\.py$"}, import_closure(entry)) == []


def test_only_a_module_pytest_runs_resolves_a_bare_import_through_pythonpath(tmp_path):
    # `filter_errors` resolves only through the `ansible/filter_plugins` pythonpath root. A
    # script prek or cron runs gets no `pythonpath`, so the same import raises there and must
    # not read as reaching the plugin; a test module's must.
    plugin = (REPO / "ansible/filter_plugins/filter_errors.py").resolve()
    script = tmp_path / "cron_job.py"
    script.write_text("import filter_errors\n")
    test = tmp_path / "test_job.py"
    test.write_text("import filter_errors\n")
    assert plugin not in imported_files(script)
    assert plugin in imported_files(test)
