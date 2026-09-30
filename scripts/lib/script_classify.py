#!/usr/bin/env python3
"""How every first-party script under ``scripts/`` is run, derived from the tree.

Split out of ``scripts/docs/reference/scripts.py`` on 2026-09-04. The generator renders the
page; this module answers the question the page is about, and it does so without importing a
single script it classifies.

WHY IT IS DERIVED RATHER THAN DECLARED. A hand-kept list of "these ones are automated" is
stale the first time someone adds a cron. The tree already says how every script is reached:
``prek.toml`` names the commit gates, ``ansible.builtin.cron`` names the scheduled ones, the
workflows name the CI ones, and the import graph names the modules that are libraries rather
than entry points. ``classify`` reads those, so the page cannot drift from the tree.

The census helpers (``candidates``, ``by_name``, ``SUFFIXES``) live here rather than in the
generator because ``classify`` is their heaviest caller, and because ``lib.script_coverage``
needs the same "which files are scripts" answer — a leaf never imports the facade it was split
out of. ``is_candidate`` is the predicate behind ``candidates``: nothing outside this module
calls it, so it stays out of ``__all__``.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import re
import tomllib
from pathlib import Path

from lib.invocation_sites import (
    claude_hook_files as _claude_hook_files,
    cron_jobs as _shared_cron_jobs,
    sh_j2_templates as _sh_j2_templates,
    workflow_files as _workflow_files,
)
from lib.repo_paths import REPO, SCRIPTS

__all__ = [
    "ARGV_RE",
    "RUNS",
    "SUFFIXES",
    "by_name",
    "candidates",
    "classify",
    "file_text",
    "importers",
]

# Not documentation about the tree: a test, a pytest fixture module, or a private helper
# whose name says it is not an entry point.
_EXCLUDED_PREFIXES = ("test_", "_")
_EXCLUDED_NAMES = {"conftest.py"}

SUFFIXES = (".py", ".sh")

# --- How a script is run --------------------------------------------------------------
#
# Four kinds, most-demanding first. A script reached more than one way takes the highest,
# because that is the one that decides how much a break costs: a scheduled script fails
# unattended at 3am, an adhoc one fails in front of the person who ran it.
RUNS = {
    "scheduled": "a cron runs it unattended",
    "gate": "every commit, CI run, deploy or Claude session runs it",
    "library": "imported by another script — not an entry point",
    "adhoc": "a person runs it",
}
_PRECEDENCE = ("adhoc", "library", "gate", "scheduled")

# A reference to a script, in any of the spellings the tree uses.
# `*` on the directory group, not `?`: a script may sit any number of directories under
# `scripts/`, and capping the path at one level made a nested one read as never invoked.
_SCRIPT_REF_RE = re.compile(
    r"(?:\./|/)?scripts/(?:[A-Za-z0-9_]+/)*([A-Za-z0-9_-]+\.(?:py|sh))"
)

# A whole string literal that is a script path and nothing else — one element of an argv
# list, as opposed to a sentence that happens to name a script.
ARGV_RE = re.compile(r"(?:\./)?scripts/(?:[A-Za-z0-9_]+/)*([A-Za-z0-9_-]+\.(?:py|sh))")

# A line that RUNS something, as opposed to one that mentions it. Every generated doc page,
# every role CLAUDE.md and a good many comments name these scripts; without this the census
# would classify by how often a script is talked about.
_RUN_CONTEXT_RE = re.compile(
    r"uv run|python|bash|/bin/sh|\bexec\b|entry\s*=|\./scripts/"
)

# A wrapper installed on the host, as a cron `job:` names it. Resolving one back to its
# template is what makes `build_docs.py` (run by docs-refresh.sh, run by a cron) scheduled
# rather than invisible.
_WRAPPER_RE = re.compile(r"([A-Za-z0-9_.-]+\.sh)\b")


def _invoked_in(text: str) -> set[str]:
    """Script filenames this text actually invokes.

    Three exclusions carry the precision. A comment line is a mention. A line carrying a
    backtick is prose citing a command, which is how every CLAUDE.md and half the role
    defaults name these scripts. A line starting with `echo` is a message about a command —
    a shell wrapper printing "or another Claude session (uv run python scripts/dev/prune_worktrees.py)"
    on lock contention, and reading that as an invocation would make an interactive tool
    look like part of the deploy path.
    """
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*")):
            continue
        if "`" in line or stripped.startswith("echo"):
            continue
        if not _RUN_CONTEXT_RE.search(line):
            continue
        found.update(_SCRIPT_REF_RE.findall(line))
    return found


def file_text(path: Path) -> str:
    """The file's text, or "" when it cannot be read or decoded."""
    try:
        return path.read_text()
    except OSError, UnicodeDecodeError:
        return ""


def _argv_references(text: str) -> set[str]:
    """Script filenames named by a string literal in Python source.

    `build_docs.py` runs the reference generators through `subprocess`, so the path is one
    element of an argv list and the word `python` is several lines away — the line scan
    cannot see it. Docstrings are skipped: every generator's own `Usage::` block names
    itself, and a `See scripts/diagnostics/probe.py` in a docstring is a mention.

    The string must be a bare path and nothing else. `session-health.py` carries the
    sentence "…the staleness gate (scripts/deploy_tools/deploy_staleness.py, exit 4)…" in a string it
    prints, and reading that as an invocation would put a deploy gate behind a session hook.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError, ValueError:
        return set()
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
        ):
            match = ARGV_RE.fullmatch(node.value.strip())
            if match:
                found.add(match.group(1))
    return found


def _literal_segments(node: ast.AST) -> list[str]:
    """The string literals a path-building expression supplies, in order.

    Two shapes, because both are written here: `os.path.join(repo, "scripts", "x.py")` and
    `root / "scripts" / "x.py"`. A segment a variable supplies contributes nothing — the
    filename still has to be spelled out for the caller to count.
    """
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "join"):
            return []
        args = node.args
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        args = [node.left, node.right]
    else:
        return []
    out: list[str] = []
    for arg in args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
        else:
            out += _literal_segments(arg)
    return out


def _constructed_path_references(text: str) -> set[str]:
    """Script filenames an expression assembles out of separate path segments.

    `deploy_io.staging_expect_script` built `os.path.join(repo, "scripts", "deploy_tools",
    "staging_expectations.py")`, so no string literal in the file spelled the filename next to
    its directory and `_argv_references` could not see it. The GitOps deployer ran both staging
    scripts that way, and the generated page called one of them "no automated caller in the
    tree" (#2424). Those callers and those scripts are retired (#2859, #2941); the shape is not
    specific to them, and this reader is what stops the next assembled path repeating it.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError, ValueError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.BinOp)):
            continue
        match = ARGV_RE.fullmatch("/".join(_literal_segments(node)))
        if match:
            found.add(match.group(1))
    return found


def _invoked_by(path: Path, scripts: Path) -> set[str]:
    """Everything one file invokes, by whichever reading its language allows."""
    text = file_text(path)
    found = _invoked_in(text)
    if path.suffix == ".py":
        found |= _argv_references(text) | _constructed_path_references(text)
    found.discard(path.name)
    return {n for n in found if n in by_name(scripts)}


def _cron_jobs(repo: Path) -> list[tuple[str, str]]:
    """(cron name, command) for every present `ansible.builtin.cron` task in the tree.

    File discovery and field extraction live in `lib.invocation_sites`, shared with
    `scripts/test_invoker_paths_resolve.py` — both need the same "which files, which
    field" answer for a cron `job:`.
    """
    return [(job.name, job.job) for job in _shared_cron_jobs(repo)]


def _wrapper_templates(repo: Path) -> dict[str, Path]:
    """Shell-wrapper basename -> the template that renders it."""
    return {path.name[: -len(".j2")]: path for path in _sh_j2_templates(repo)}


def _scheduled(repo: Path) -> dict[str, str]:
    """Script filename -> the cron that reaches it, directly or through a wrapper."""
    wrappers = _wrapper_templates(repo)
    reached: dict[str, str] = {}
    for name, command in _cron_jobs(repo):
        for script in _invoked_in(command):
            reached.setdefault(script, name)
        for wrapper in _WRAPPER_RE.findall(command):
            template = wrappers.get(wrapper)
            if template is None:
                continue
            for script in _invoked_in(file_text(template)):
                reached.setdefault(script, f"{name} (via {wrapper})")
    return reached


def _invocation_sites(repo: Path) -> list[tuple[Path, str, str]]:
    """(file, kind, evidence) for every file in the tree that can invoke a script.

    Nothing in `scripts/` is reached from a systemd unit — every `ExecStart` in the tree
    runs a host binary or a role's own `files/` module — so units are not scanned.
    """
    sites: list[tuple[Path, str, str]] = []

    prek = repo / "prek.toml"
    if prek.is_file():
        sites.append((prek, "gate", "prek hook (every commit)"))

    for path in _workflow_files(repo):
        sites.append((path, "gate", f"CI: {path.name}"))

    for path in _claude_hook_files(repo):
        sites.append((path, "gate", f"Claude hook: {path.name}"))

    # deploy.sh is the interactive deploy path: what it runs, every deploy runs.
    deploy = repo / "scripts" / "deploy.sh"
    if deploy.is_file():
        sites.append((deploy, "gate", "every deploy (deploy.sh)"))

    ansible = repo / "ansible"
    for pattern in ("roles/**/tasks/*.yml", "roles/**/templates/*", "roles/**/files/*"):
        for path in sorted(ansible.glob(pattern)):
            if not path.is_file():
                continue
            if path.name.startswith("test_"):
                continue
            rel = path.relative_to(repo).as_posix()
            sites.append((path, "gate", f"deploy: {rel}"))

    # A top-level playbook or bring-up script is something a person runs on purpose.
    for path in sorted(ansible.glob("*.yml")) + sorted(ansible.glob("*.sh")):
        sites.append((path, "adhoc", f"playbook: {path.relative_to(repo).as_posix()}"))

    return sites


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
    root. Resolving against `scripts/` alone read those ten modules as imported by nobody
    (#3020). The roots stop at `scripts/`: looking further up would let a directory outside the
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
    `files/`, not `scripts/lib/gitops_markers.py` (#3038).
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


def _import_graph(scripts: Path, keep) -> dict[str, set[str]]:
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
        # credited `scripts/docs/reference/secrets.py` with `import secrets` (#3038).
        roots = _roots(path, scripts) + _path_inserts(tree, path)
        if _is_test_file(path):
            roots += pythonpath
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [_head(alias.name, roots, scripts) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import resolves against the package directory `node.level` says,
                # and against nothing else. `from .citations import Citation` in
                # `scripts/lib/facts/atoms.py` is how a package's own members reach each other,
                # and skipping those read `citations.py` as a script nobody runs (#3020).
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


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_") or path.name in _EXCLUDED_NAMES


def importers(scripts: Path) -> dict[str, set[str]]:
    """Module stem -> the non-test scripts that import it.

    A test importing its subject does not make the subject a library, so `test_*` and
    `conftest` are not importers here.
    """
    return _import_graph(scripts, lambda path: not _is_test_file(path))


def _test_importers(scripts: Path) -> dict[str, set[str]]:
    """Module stem -> the test files that import it.

    A module only a test imports is not a library by the rule above, but a Python module with
    no `__main__` guard is not something a person runs either: there is nothing to run.
    `classify` uses this to say which tests reach it instead of calling it uninvoked.
    """
    return _import_graph(scripts, _is_test_file)


def _has_main_guard(text: str) -> bool:
    """Whether Python source has a module-level `if __name__ == ...` block of its own."""
    try:
        tree = ast.parse(text)
    except SyntaxError, ValueError:
        return False
    return any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in tree.body
    )


def classify(repo: Path = REPO, scripts: Path = SCRIPTS) -> dict[str, tuple[str, str]]:
    """Script filename -> (how it runs, the evidence for saying so)."""
    verdicts: dict[str, tuple[str, str]] = {}

    def record(script: str, kind: str, evidence: str) -> bool:
        current = verdicts.get(script)
        if current and _PRECEDENCE.index(current[0]) >= _PRECEDENCE.index(kind):
            return False
        verdicts[script] = (kind, evidence)
        return True

    for script, cron in _scheduled(repo).items():
        record(script, "scheduled", f"cron: {cron}")

    for path, kind, evidence in _invocation_sites(repo):
        for script in _invoked_by(path, scripts):
            record(script, kind, evidence)

    imported = importers(scripts)
    for stem, callers in imported.items():
        record(f"{stem}.py", "library", f"imported by {', '.join(sorted(callers))}")

    # A SCRIPT another one imports and calls in process -- `deploy_run.py` running
    # `deploy_staleness.main` on every deploy -- runs as often as that caller does, exactly as
    # it did when the caller spawned it. Only a module with its own `__main__` guard counts:
    # a plain library module has no run of its own to inherit.
    by_path = by_name(scripts)
    entry_points = {
        stem
        for stem in imported
        if f"{stem}.py" in by_path and _has_main_guard(file_text(by_path[f"{stem}.py"]))
    }
    runs_in_process: dict[str, set[str]] = {}
    for stem in entry_points:
        for caller in imported[stem]:
            runs_in_process.setdefault(caller, set()).add(f"{stem}.py")

    # One script running another inherits the caller's kind, so the six reference
    # generators are scheduled by way of `build_docs.py` and its cron rather than reading
    # as things nobody runs. Iterated to a fixpoint: the chain is cron → build_docs.py →
    # generator, and a longer one would otherwise resolve only as far as it was walked.
    callers = {
        path: _invoked_by(path, scripts) | runs_in_process.get(path.name, set())
        for path in candidates(scripts)
    }
    while True:
        settled = True
        for path, targets in callers.items():
            kind = verdicts.get(path.name, ("adhoc", ""))[0]
            if kind == "library":
                # A library is reached through whoever imports it; propagating "library"
                # onto something it shells out to would say the wrong thing.
                kind = "adhoc"
            for target in targets:
                if record(target, kind, f"{path.name} ({RUNS[kind]})"):
                    settled = False
        if settled:
            break

    # A Python module with no `__main__` guard that only a test imports is not a library by
    # the rule above, and it is not something a person runs either -- there is nothing to run.
    # `grafana_panel_report.py` is the live case: its classifier is unit-tested without a
    # browser, and the page called it "no automated caller in the tree" (#3020).
    tested = _test_importers(scripts)
    for path in candidates(scripts):
        if path.suffix != ".py" or path.name in verdicts:
            continue
        callers = tested.get(path.stem)
        if callers and not _has_main_guard(file_text(path)):
            record(
                path.name,
                "library",
                f"no `__main__` guard; imported by {', '.join(sorted(callers))}",
            )

    for path in candidates(scripts):
        verdicts.setdefault(path.name, ("adhoc", "no automated caller in the tree"))
    return verdicts


def is_candidate(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix in SUFFIXES
        and path.name not in _EXCLUDED_NAMES
        and not path.name.startswith(_EXCLUDED_PREFIXES)
    )


def _walk(scripts: Path) -> list[Path]:
    """Every file under scripts/, at any depth, skipping compiled caches."""
    return sorted(p for p in scripts.rglob("*") if "__pycache__" not in p.parts)


def _all_py(scripts: Path) -> list[Path]:
    """Every .py under scripts/, including the tests — the import scan needs their stems."""
    return [p for p in _walk(scripts) if p.suffix == ".py"]


def candidates(scripts: Path) -> list[Path]:
    """Every first-party script under scripts/, at any depth.

    Depth is not capped. It was capped at one directory until 2026-09-02, which meant a
    module in a nested subdirectory was absent from the page entirely rather than listed
    as uncovered — the failure mode a reference page must not have.

    Filenames stay unique across the subdirectories, which is what lets the rest of this
    module key verdicts and evidence on the bare name rather than a path. That is now an
    enforced invariant, not an assumption: see
    `test_no_two_scripts_share_a_basename` in
    `scripts/docs/tests/test_gen_reference_scripts.py` for why keying by path
    would relocate the ambiguity rather than remove it.
    """
    return sorted((p for p in _walk(scripts) if is_candidate(p)), key=lambda p: p.name)


def by_name(scripts: Path) -> dict[str, Path]:
    return {p.name: p for p in candidates(scripts)}
