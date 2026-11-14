#!/usr/bin/env python3
"""How every first-party script under ``scripts/`` is run, derived from the tree.

The generator renders the page; this module answers the question the page is about, and it
does so without importing a single script it classifies.

WHY IT IS DERIVED RATHER THAN DECLARED. A hand-kept list of "these ones are automated" is
stale the first time someone adds a timer. The tree already says how every script is reached:
``prek.toml`` names the commit gates, the host components' timers name the scheduled ones,
the workflows name the CI ones, and the import graph names the modules that are libraries rather
than entry points. ``classify`` reads those, so the page cannot drift from the tree.

The census helpers (``candidates``, ``by_name``) live here rather than in the generator
because ``classify`` is their heaviest caller, and because the ``--help`` test needs the same
"which files are scripts" answer without importing the generator. ``is_candidate`` is the
predicate behind ``candidates``: nothing outside this module calls it, so it stays out of
``__all__``.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import functools
import re
from pathlib import Path

from lib.invocation_sites import (
    claude_hook_files as _claude_hook_files,
    workflow_files as _workflow_files,
)
from lib.repo_paths import REPO, SCRIPTS
from lib.script_imports import (
    import_graph as _import_graph,
    is_test_file as _is_test_file,
)

__all__ = [
    "ARGV_RE",
    "RUNS",
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
# A Job's ConfigMap carries these files, and a pod runs them. No person or cron runs one, and
# editing one renames the Job, so the entry-point rules do not reach them.
_JOB_FILES = "job_files"

SUFFIXES = (".py", ".sh")

# --- How a script is run --------------------------------------------------------------
#
# Four kinds, most-demanding first. A script reached more than one way takes the highest,
# because that is the one that decides how much a break costs: a scheduled script fails
# unattended at 3am, an adhoc one fails in front of the person who ran it.
RUNS = {
    "scheduled": "a timer runs it unattended",
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


def _nodes(text: str) -> list[ast.AST]:
    """Every node of Python source, walked once, or none when it does not parse.

    Both readers below scan the whole tree, and `ast.walk` costs more than either scan: a
    `classify()` of the real tree spent most of its time walking each file once per reader.
    """
    try:
        return list(ast.walk(ast.parse(text)))
    except SyntaxError, ValueError:
        return []


def _argv_references(nodes: list[ast.AST]) -> set[str]:
    """Script filenames named by a string literal in Python source, given as its `_nodes`.

    `build_docs.py` runs the reference generators through `subprocess`, so the path is one
    element of an argv list and the word `python` is several lines away — the line scan
    cannot see it. Docstrings are skipped: every generator's own `Usage::` block names
    itself, and a `See scripts/diagnostics/probe.py` in a docstring is a mention.

    The string must be a bare path and nothing else. `session-health.py` carries the
    sentence "…the staleness gate (scripts/deploy_tools/land_lib/classify.py, exit 4)…" in a
    string it prints, and reading that as an invocation would put a deploy gate behind a session hook.

    A dict key is skipped too. It names a file as data, never as an argv element: the
    retired deployer's `SETUP_FILES_SHIPPED_BY_OTHER_ROLES` was keyed by module paths.
    """
    keys = {key for node in nodes if isinstance(node, ast.Dict) for key in node.keys}
    docstrings = {
        node.body[0].value
        for node in nodes
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    # A `*_PREFIXES` entry is matched against a diff, never run; an argv list is not skipped.
    prefixes = {
        element
        for module in nodes
        if isinstance(module, ast.Module)
        for stmt in module.body
        if isinstance(stmt, ast.Assign)
        and isinstance(stmt.value, (ast.Tuple, ast.List))
        and any(
            isinstance(t, ast.Name) and t.id.endswith("_PREFIXES") for t in stmt.targets
        )
        for element in stmt.value.elts
    }
    found: set[str] = set()
    for node in nodes:
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and node not in keys
            and node not in prefixes
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


def _constructed_path_references(nodes: list[ast.AST]) -> set[str]:
    """Script filenames an expression assembles out of separate path segments, from `_nodes`.

    A caller can build `os.path.join(repo, "scripts", "deploy_tools",
    "staging_expectations.py")`, so no string literal in the file spells the filename next to
    its directory and `_argv_references` cannot see it. The generated page then calls the
    script "no automated caller in the tree". This reader is what stops an assembled path
    from reading that way.
    """
    found: set[str] = set()
    for node in nodes:
        if not isinstance(node, (ast.Call, ast.BinOp)):
            continue
        match = ARGV_RE.fullmatch("/".join(_literal_segments(node)))
        if match:
            found.add(match.group(1))
    return found


def _invoked_by(path: Path, catalogue: dict[str, Path]) -> set[str]:
    """Everything one file invokes, by whichever reading its language allows.

    Args:
        path: The file to read.
        catalogue: `by_name(scripts)`, built once by the caller. Rebuilding it here walked
            `scripts/` once per file read, which was most of `classify`'s run time.
    """
    text = file_text(path)
    found = _invoked_in(text)
    if path.suffix == ".py":
        nodes = _nodes(text)
        found |= _argv_references(nodes) | _constructed_path_references(nodes)
    found.discard(path.name)
    return {n for n in found if n in catalogue}


def _component_timer_jobs(repo: Path) -> dict[str, str]:
    """Script filename -> the host-component unit that reaches it (H7.10).

    A timer's service in `hosts/components/*/units/` runs a script directly, or a shipped
    wrapper whose template sits in the same component. The wrapper installs under
    `/usr/lib/homelab/<name>`, so a wrapper is matched by the template's stem.
    """
    components = repo / "hosts" / "components"
    wrappers = {
        path.name.removesuffix(".sh.j2"): path
        for path in components.glob("*/*/*.sh.j2")
    }
    reached: dict[str, str] = {}
    for unit in sorted(components.glob("*/units/*.service*")):
        text = file_text(unit)
        for line in text.splitlines():
            if not line.startswith("ExecStart"):
                continue
            command = line.partition("=")[2]
            for script in _invoked_in(command):
                reached.setdefault(script, unit.name)
            wrapper = wrappers.get(Path(command.split()[0].lstrip("+")).name)
            if wrapper is not None:
                for script in _invoked_in(file_text(wrapper)):
                    reached.setdefault(script, f"{unit.name} (via {wrapper.name})")
    return reached


def _invocation_sites(repo: Path) -> list[tuple[Path, str, str]]:
    """(file, kind, evidence) for every file in the tree that can invoke a script.

    Systemd units are not scanned here: the units that reach `scripts/` since H7.10 are the
    host components' timers, which `_component_timer_jobs` reads.
    """
    sites: list[tuple[Path, str, str]] = []

    prek = repo / "prek.toml"
    if prek.is_file():
        sites.append((prek, "gate", "prek hook (every commit)"))

    for path in _workflow_files(repo):
        sites.append((path, "gate", f"CI: {path.name}"))

    for path in _claude_hook_files(repo):
        sites.append((path, "gate", f"Claude hook: {path.name}"))

    roles = repo / "k8s" / "roles"
    # `files/*/*` reaches a package a role ships from a subdirectory, such as the retired
    # gitops_deploy role's `files/deployer_lib/` (#4355).
    patterns = (
        "**/templates/*",
        "**/files/*",
        "**/files/*/*",
    )
    for pattern in patterns:
        for path in sorted(roles.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.name.startswith("test_"):
                continue
            rel = path.relative_to(repo).as_posix()
            sites.append((path, "gate", f"deploy: {rel}"))

    return sites


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


def _main_shape(text: str) -> tuple[bool, bool]:
    """(has a module-level `if __name__ == ...` block, defines a module-level `main`).

    Both are False when the source does not parse.
    """
    try:
        body = ast.parse(text).body
    except SyntaxError, ValueError:
        return False, False
    guarded = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in body
    )
    return guarded, any(
        isinstance(n, ast.FunctionDef) and n.name == "main" for n in body
    )


def _has_main_guard(text: str) -> bool:
    """Whether Python source has a module-level `if __name__ == ...` block of its own."""
    return _main_shape(text)[0]


def classify(repo: Path = REPO, scripts: Path = SCRIPTS) -> dict[str, tuple[str, str]]:
    """Script filename -> (how it runs, the evidence for saying so)."""
    verdicts: dict[str, tuple[str, str]] = {}
    # Each script is read once per question for this call. Three checks below ask its
    # `_main_shape`, and a dispatch target is asked what it invokes twice; the repeats were
    # a sixth of this function's time (#5564).
    shape = functools.cache(lambda path: _main_shape(file_text(path)))

    def record(script: str, kind: str, evidence: str) -> bool:
        current = verdicts.get(script)
        if current and _PRECEDENCE.index(current[0]) >= _PRECEDENCE.index(kind):
            return False
        verdicts[script] = (kind, evidence)
        return True

    for script, unit in _component_timer_jobs(repo).items():
        record(script, "scheduled", f"timer: {unit}")

    catalogue = candidates(scripts)
    by_path = {p.name: p for p in catalogue}
    invoked = functools.cache(lambda path: _invoked_by(path, by_path))
    for path, kind, evidence in _invocation_sites(repo):
        for script in invoked(path):
            record(script, kind, evidence)

    imported = importers(scripts)
    for stem, callers in imported.items():
        record(f"{stem}.py", "library", f"imported by {', '.join(sorted(callers))}")

    # A SCRIPT another one imports and calls in process runs as often as that caller does,
    # exactly as it did when the caller spawned it. Only a module with its own `__main__`
    # guard counts: a plain library module has no run of its own to inherit.
    entry_points = {
        stem
        for stem in imported
        if f"{stem}.py" in by_path and shape(by_path[f"{stem}.py"])[0]
    }
    runs_in_process: dict[str, set[str]] = {}
    for stem in entry_points:
        for caller in imported[stem]:
            runs_in_process.setdefault(caller, set()).add(f"{stem}.py")

    # A dispatcher's target has a module-level `main` and no guard. It stays a library itself,
    # since no person runs it, but what it runs is credited to its importer (#4347).
    for stem, stem_callers in imported.items():
        path = by_path.get(f"{stem}.py")
        # A dispatch target: a module-level `main` and no `__main__` guard.
        if path is None or shape(path) != (False, True):
            continue
        runs = runs_in_process.get(f"{stem}.py", set()) | invoked(path)
        for caller in stem_callers:
            runs_in_process.setdefault(caller, set()).update(runs)

    # One script running another inherits the caller's kind, so the six reference
    # generators are scheduled by way of `build_docs.py` and its timer rather than reading
    # as things nobody runs. Iterated to a fixpoint: the chain is timer → build_docs.py →
    # generator, and a longer one would otherwise resolve only as far as it was walked.
    callers = {
        path: invoked(path) | runs_in_process.get(path.name, set())
        for path in catalogue
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
    # browser, and the page called it "no automated caller in the tree".
    tested = _test_importers(scripts)
    for path in catalogue:
        if path.suffix != ".py" or path.name in verdicts:
            continue
        callers = tested.get(path.stem)
        if callers and not shape(path)[0]:
            record(
                path.name,
                "library",
                f"no `__main__` guard; imported by {', '.join(sorted(callers))}",
            )

    for path in catalogue:
        verdicts.setdefault(path.name, ("adhoc", "no automated caller in the tree"))
    return verdicts


def is_candidate(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix in SUFFIXES
        and path.name not in _EXCLUDED_NAMES
        and not path.name.startswith(_EXCLUDED_PREFIXES)
        and _JOB_FILES not in path.parts
    )


def _walk(scripts: Path) -> list[Path]:
    """Every file under scripts/, at any depth, skipping compiled caches."""
    return sorted(p for p in scripts.rglob("*") if "__pycache__" not in p.parts)


def candidates(scripts: Path) -> list[Path]:
    """Every first-party script under scripts/, at any depth.

    Depth is not capped, so a module in a nested subdirectory is listed as uncovered rather
    than absent from the page entirely — the failure mode a reference page must not have.

    Filenames stay unique across the subdirectories, which is what lets the rest of this
    module key verdicts and evidence on the bare name rather than a path. That is an
    enforced invariant, not an assumption: see
    `test_no_two_scripts_share_a_basename` in
    `scripts/docs/tests/test_gen_reference_scripts.py` for why keying by path
    would relocate the ambiguity rather than remove it.
    """
    return sorted((p for p in _walk(scripts) if is_candidate(p)), key=lambda p: p.name)


def by_name(scripts: Path) -> dict[str, Path]:
    return {p.name: p for p in candidates(scripts)}
