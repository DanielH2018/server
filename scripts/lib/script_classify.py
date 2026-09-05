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

The census helpers (``candidates``, ``by_name``, ``is_candidate``, ``SUFFIXES``) live here
rather than in the generator because ``classify`` is their heaviest caller, and because
``lib.script_coverage`` needs the same "which files are scripts" answer — a leaf never imports
the facade it was split out of.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import re
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
    "is_candidate",
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
    `deploy.sh` prints "or another Claude session (uv run python scripts/dev/prune_worktrees.py)"
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


def _invoked_by(path: Path, scripts: Path) -> set[str]:
    """Everything one file invokes, by whichever reading its language allows."""
    text = file_text(path)
    found = _invoked_in(text)
    if path.suffix == ".py":
        found |= _argv_references(text)
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
            if not path.is_file() or "/archive/" in path.as_posix():
                continue
            if path.name.startswith("test_"):
                continue
            rel = path.relative_to(repo).as_posix()
            sites.append((path, "gate", f"deploy: {rel}"))

    # A top-level playbook or bring-up script is something a person runs on purpose.
    for path in sorted(ansible.glob("*.yml")) + sorted(ansible.glob("*.sh")):
        sites.append((path, "adhoc", f"playbook: {path.relative_to(repo).as_posix()}"))

    return sites


def _importers(scripts: Path) -> dict[str, set[str]]:
    """Module stem -> the non-test scripts that import it.

    A test importing its subject does not make the subject a library, so `test_*` and
    `conftest` are not importers here.
    """
    stems = {p.stem for p in _all_py(scripts)}

    def _module(dotted: str) -> str:
        """The module stem inside a dotted import path, or "" if it names only directories.

        A cross-directory import is spelled `from lib.docs_provenance import ...` — the module
        sought is not the first segment, which names a directory. Walk past EVERY leading
        segment that is a real directory rather than assuming one: `diagnostics.probe_lib.core`
        has two, and stopping at the second reported a module twelve scripts import as
        something nobody runs.
        """
        parts = dotted.split(".")
        here, i = scripts, 0
        while i < len(parts) and (here / parts[i]).is_dir():
            here /= parts[i]
            i += 1
        return parts[i] if i < len(parts) else ""

    importers: dict[str, set[str]] = {}
    for path in _all_py(scripts):
        if path.name.startswith("test_") or path.name in _EXCLUDED_NAMES:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError, ValueError, UnicodeDecodeError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [_module(alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    names = []
                else:
                    # `from diagnostics.probe_lib import core` names only directories in the
                    # module part, so the modules imported are the aliases.
                    head = _module(node.module)
                    names = [head] if head else [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                if name in stems and name != path.stem:
                    importers.setdefault(name, set()).add(path.name)
    return importers


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

    for stem, callers in _importers(scripts).items():
        record(f"{stem}.py", "library", f"imported by {', '.join(sorted(callers))}")

    # One script running another inherits the caller's kind, so the six reference
    # generators are scheduled by way of `build_docs.py` and its cron rather than reading
    # as things nobody runs. Iterated to a fixpoint: the chain is cron → build_docs.py →
    # generator, and a longer one would otherwise resolve only as far as it was walked.
    callers = {path: _invoked_by(path, scripts) for path in candidates(scripts)}
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
