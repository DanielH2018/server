#!/usr/bin/env python3
"""Generate docs/reference/scripts.md — every first-party script and what it is for.

WHY THIS PAGE IS GENERATED. There are around 40 scripts in scripts/, and every one already
carries a module docstring that says what it does — several with a `Usage::` block. A
hand-written index of them is stale the day someone adds the forty-first. The docstrings
already ARE the documentation; this assembles them.

WHY IT PARSES AND NEVER IMPORTS. Reading a docstring by importing the module runs its
top-level code. Across this directory that would mean dialling hosts, taking locks and
resolving SOPS on every docs refresh. `ast.parse` plus `ast.get_docstring` reads the same
string and executes nothing. A fixture script whose body raises at module level pins that
in the tests.

WHAT IT REPORTS RATHER THAN HIDES. A script that does not parse, and a script with no
docstring, both get a row saying so. Dropping them would make the page quietly incomplete,
which is worse than a visible gap. The same goes for the test column: a script with no
`test_<name>.py` shows an empty cell, because an untested script is a fact worth surfacing.

HOW EACH SCRIPT IS RUN IS DERIVED, NOT DECLARED. A hand-kept list of "these ones are
automated" is stale the first time someone adds a cron. The tree already says how every
script is reached: `prek.toml` names the commit gates, `ansible.builtin.cron` names the
scheduled ones, the workflows name the CI ones, and the import graph names the modules that
are libraries rather than entry points. `classify()` reads those, so the page cannot drift
from the tree.

WHAT IT CANNOT DECIDE. Whether a script is safe to run. The summary is whatever its author
wrote, and nothing here judges blast radius — `docs/reference/crons.md` does that for the
scheduled ones.

Usage::

    uv run python scripts/docs/gen_reference_scripts.py --out docs/reference/scripts.md
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.docs_provenance import md_cell as _md_cell
from lib.invocation_sites import (
    claude_hook_files as _claude_hook_files,
    cron_jobs as _shared_cron_jobs,
    sh_j2_templates as _sh_j2_templates,
    workflow_files as _workflow_files,
)
from lib.repo_paths import REPO, SCRIPTS

# Not documentation about the tree: a test, a pytest fixture module, or a private helper
# whose name says it is not an entry point.
_EXCLUDED_PREFIXES = ("test_", "_")
_EXCLUDED_NAMES = {"conftest.py"}

_SUFFIXES = (".py", ".sh")

# The reStructuredText usage marker the repo's scripts already use, and the indented block
# that follows it.
_USAGE_RE = re.compile(r"^Usage::\s*$", re.MULTILINE)


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
_SCRIPT_REF_RE = re.compile(
    r"(?:\./|/)?scripts/(?:[A-Za-z0-9_]+/)?([A-Za-z0-9_-]+\.(?:py|sh))"
)

# A whole string literal that is a script path and nothing else — one element of an argv
# list, as opposed to a sentence that happens to name a script.
_ARGV_RE = re.compile(r"(?:\./)?scripts/(?:[A-Za-z0-9_]+/)?([A-Za-z0-9_-]+\.(?:py|sh))")

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


def _read(path: Path) -> str:
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
            match = _ARGV_RE.fullmatch(node.value.strip())
            if match:
                found.add(match.group(1))
    return found


def _invoked_by(path: Path, scripts: Path) -> set[str]:
    """Everything one file invokes, by whichever reading its language allows."""
    text = _read(path)
    found = _invoked_in(text)
    if path.suffix == ".py":
        found |= _argv_references(text)
    found.discard(path.name)
    return {n for n in found if n in _by_name(scripts)}


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
            for script in _invoked_in(_read(template)):
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
    # A cross-directory import is spelled `from lib.docs_provenance import ...` — the module
    # sought is the SECOND segment. Reading only the first would see `lib`, match nothing, and
    # report a module every generator imports as something nobody runs.
    subdirs = {d.name for d in scripts.iterdir() if d.is_dir()}

    def _module(dotted: str) -> str:
        head, _, rest = dotted.partition(".")
        return rest.partition(".")[0] if head in subdirs and rest else head

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
                names = [_module(node.module)] if node.module and not node.level else []
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
    callers = {
        path: _invoked_by(path, scripts)
        for path in _candidates(scripts)
        if _is_candidate(path)
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

    for path in _candidates(scripts):
        if _is_candidate(path):
            verdicts.setdefault(path.name, ("adhoc", "no automated caller in the tree"))
    return verdicts


def _python_docstring(path: Path) -> str | None:
    """The module docstring, or None if the file does not parse."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError, ValueError, UnicodeDecodeError:
        return None
    return ast.get_docstring(tree) or ""


def _shell_docstring(path: Path) -> str:
    """The leading `#` comment block, shebang excluded."""
    lines = []
    for line in path.read_text().splitlines():
        if line.startswith("#!"):
            continue
        if line.startswith("#"):
            lines.append(line.lstrip("#").strip())
            continue
        if not line.strip() and not lines:
            continue
        break
    return "\n".join(lines)


def _usage(doc: str) -> str:
    """The indented block after a `Usage::` marker, dedented. Empty when absent."""
    match = _USAGE_RE.search(doc)
    if not match:
        return ""
    block = []
    for line in doc[match.end() :].splitlines():
        if not line.strip():
            if block:
                break
            continue
        if not line.startswith((" ", "\t")):
            break
        block.append(line.strip())
    return "\n".join(block)


def _test_files(repo: Path, scripts: Path) -> list[Path]:
    """Every pytest file that could be about a script, in any of the test roots.

    A subdirectory's tests live either beside its modules (`scripts/<dir>/test_*.py`, not
    yet split) or in its own `tests/` sibling (`scripts/<dir>/tests/test_*.py`, the split
    layout) — both are covered so a mid-migration tree and a finished one both scan clean.
    """
    return (
        sorted(scripts.glob("test_*.py"))
        + sorted(scripts.glob("*/test_*.py"))
        + sorted(scripts.glob("*/tests/test_*.py"))
        + sorted((repo / "ansible" / "tests").rglob("test_*.py"))
    )


def _indirect_test(name: str, test_files: list[Path], scripts: Path) -> tuple[str, str]:
    """The test that names this script but is not called `test_<name>.py`.

    `gitops_tick.sh` has five tests, in `ansible/tests/deploy/test_gitops_manual_trigger.py`, and
    reporting it as untested on the naming convention alone put a covered script on the gap
    list. A module with no test file of its own is likewise often reached through the entry
    point that imports it.

    The two mechanisms are not equally trustworthy, so they are not treated alike.

    An import is real exercise, and is credited wherever it appears. A path string is only a
    mention, and a mention inside `test_<other script>.py` is that other script's test
    talking about this one — `test_deploy_detach_notify.py` opens "the `scripts/deploy.sh`
    --detach completion notifier", which credited 15 KB of shell that runs on every deploy to
    a test of the notifier. Matching the bare stem credited `deploy.sh` to a test that merely
    says "deploy", and credited three scripts to THIS generator's own test, which names every
    script in the tree by construction.
    """
    stem = re.escape(Path(name).stem)
    path_re = re.compile(rf"scripts/(?:[A-Za-z0-9_]+/)?{re.escape(name)}")
    if not name.endswith(".py"):
        import_re = None
    elif Path(name).stem.isidentifier():
        # Two spellings reach the same module. `import live` and `from live import x` name
        # it as a top-level module; `from infra_map import live` names it as a member of its
        # package, which is how a module in a subdirectory that shares its basename with
        # another one has to be imported. Matching only the first reported infra_map's `live`
        # and `render` as untested the moment they took the package form.
        import_re = re.compile(
            rf"^\s*(?:from|import)\s+{stem}\b"
            rf"|^\s*from\s+[\w.]+\s+import\s+.*\b{stem}\b",
            re.MULTILINE,
        )
    else:
        # A hyphenated filename is not a valid module name, so NO import statement can
        # name it — `glenstone-bot.py` is loadable only via spec_from_file_location. Its
        # test quotes the filename, which is a load and not a mention, so it counts as an
        # import. Requiring a bare `import` here reported two genuinely tested bots as
        # untested, which is the page telling a story about coverage that isn't true.
        import_re = re.compile(rf"""['"]{re.escape(name)}['"]""")
    for path in test_files:
        text = _read(path)
        if import_re and import_re.search(text):
            return path.name, "import"
    for path in test_files:
        if path_re.search(_read(path)) and not _is_another_scripts_test(path, scripts):
            return path.name, "path"
    return "", ""


def _is_another_scripts_test(test: Path, scripts: Path) -> bool:
    """Is this test named after some script in `scripts/`, rather than about the tree?"""
    subject = test.name[len("test_") :]
    known = _by_name(scripts)
    return any(f"{Path(subject).stem}{suffix}" in known for suffix in _SUFFIXES)


def _is_candidate(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix in _SUFFIXES
        and path.name not in _EXCLUDED_NAMES
        and not path.name.startswith(_EXCLUDED_PREFIXES)
    )


def _all_py(scripts: Path) -> list[Path]:
    """Every .py under scripts/, including the tests — the import scan needs their stems."""
    return sorted(list(scripts.glob("*.py")) + list(scripts.glob("*/*.py")))


def _candidates(scripts: Path) -> list[Path]:
    """Every first-party script, at the scripts/ root or one directory down.

    Filenames stay unique across the subdirectories, which is what lets the rest of this
    module keep keying verdicts and evidence on the bare name rather than a path.
    """
    found = list(scripts.glob("*")) + list(scripts.glob("*/*"))
    return sorted((p for p in found if _is_candidate(p)), key=lambda p: p.name)


def _by_name(scripts: Path) -> dict[str, Path]:
    return {p.name: p for p in _candidates(scripts)}


def build_rows(scripts: Path = SCRIPTS, repo: Path = REPO) -> list[dict[str, str]]:
    """One row per first-party script, sorted by name."""
    verdicts = classify(repo, scripts)
    test_files = _test_files(repo, scripts)
    rows = []
    for path in _candidates(scripts):
        if not _is_candidate(path):
            continue

        if path.suffix == ".py":
            doc = _python_docstring(path)
            if doc is None:
                summary, usage = f"({path.name} could not be parsed)", ""
            elif not doc.strip():
                summary, usage = "(no module docstring)", ""
            else:
                summary, usage = doc.strip().splitlines()[0].strip(), _usage(doc)
        else:
            doc = _shell_docstring(path)
            # The first NON-EMPTY line: two of these scripts open `#!`, then a bare `#`,
            # then the sentence. Taking line one left them with a blank summary cell.
            lines = [line for line in doc.splitlines() if line.strip()]
            summary = lines[0].strip() if lines else "(no leading comment)"
            # Two of them open "name.sh — what it does"; the name is already the row label.
            summary = re.sub(rf"^{re.escape(path.name)}\s+[—-]\s*", "", summary)
            usage = _usage(doc)

        # The split layout keeps a script's test in a sibling `tests/`; the flat one beside it.
        direct = path.parent / "tests" / f"test_{path.stem}.py"
        if not direct.is_file():
            direct = path.parent / f"test_{path.stem}.py"
        if direct.is_file():
            test, indirect, via = direct.name, "", ""
        else:
            test = ""
            indirect, via = _indirect_test(path.name, test_files, scripts)
        run, evidence = verdicts.get(
            path.name, ("adhoc", "no automated caller in the tree")
        )
        rows.append(
            {
                "name": path.name,
                "path": str(path.relative_to(scripts.parent)),
                "summary": summary,
                "usage": usage,
                "tests": test,
                "indirect_tests": indirect,
                "indirect_via": via,
                "run": run,
                "evidence": evidence,
            }
        )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    """Render `rows` as the "Scripts" reference page, grouped by how each script is run.

    Splits the rows into scheduled / gate / library / adhoc sections, calls out scripts that
    run unattended with no test coverage, and appends a usage block for each script that
    documents its own invocation.

    Args:
        rows: Script rows as returned by `build_rows`.

    Returns:
        The full page as Markdown text, ending in a single trailing newline.
    """
    from lib.docs_provenance import generated_banner

    by_run = {kind: [r for r in rows if r["run"] == kind] for kind in RUNS}
    unattended = by_run["scheduled"] + by_run["gate"]

    def uncovered(row: dict[str, str]) -> bool:
        return not row["tests"] and not row["indirect_tests"]

    gaps = [r for r in unattended if uncovered(r)]

    parts = [generated_banner("scripts/docs/gen_reference_scripts.py")]
    parts.append("# Scripts\n")
    parts.append(
        f"{len(rows)} first-party script(s) in `scripts/`. Each summary is the script's own "
        "module docstring — change the docstring to change this page.\n"
    )
    parts.append(
        "The sections below split them by **how each one is run**, which is derived from the "
        "tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a "
        "Claude hook, an Ansible task, or an import edge. The *Reached by* column is the "
        "evidence, so a wrong answer is a wrong answer about a real file.\n"
    )
    parts.append(
        '!!! note "What this page does not tell you"\n'
        "    Whether a script is safe to run. The summary is whatever its author wrote, and "
        "nothing here judges blast radius. For the ones that run unattended, and which of "
        "those change state, see [Scheduled jobs](crons.md).\n"
    )
    untested = [r for r in rows if uncovered(r)]
    parts.append(
        f"\n**{len(gaps)} of the {len(unattended)} scripts that run unattended have no test; "
        f"{len(untested)} of all {len(rows)} do not.** The first number is the one that "
        "matters. An untested script a person runs fails in front of that person; an untested "
        "one a cron or a commit gate runs fails unattended, or blocks everybody.\n"
    )
    parts.append(
        '!!! note "Where the Tests column looks"\n'
        "    First for a `scripts/test_<name>.py`. Failing that, for any test in `scripts/` or "
        "`ansible/tests/` that names the script — `gitops_tick.sh` has five, in "
        "`test_gitops_manual_trigger.py`, and the naming convention alone called it untested. "
        "Those show as *(indirect)*, which means a test exercises it, not that the test is "
        "about it.\n"
    )
    if gaps:
        parts.append(
            "".join(f"\n- `{row['path']}` — {row['evidence']}" for row in gaps) + "\n"
        )

    for kind, heading in (
        ("scheduled", "Run automatically, on a schedule"),
        ("gate", "Run automatically, on a commit, CI run, deploy or session"),
        ("library", "Imported, never run on their own"),
        ("adhoc", "Run by hand"),
    ):
        section = by_run[kind]
        parts.append(f"\n## {heading}\n")
        parts.append(f"{len(section)} script(s) — {RUNS[kind]}.\n")
        if not section:
            parts.append("None.\n")
            continue
        parts.append("| Script | What it does | Reached by | Tests |")
        parts.append("|---|---|---|---|")
        for row in section:
            if row["tests"]:
                test = f"`{row['tests']}`"
            elif row["indirect_tests"]:
                test = f"`{row['indirect_tests']}` *(indirect)*"
            else:
                test = "—"
            parts.append(
                f"| `{row['path']}` | {_md_cell(row['summary'])} | "
                f"{_md_cell(row['evidence'])} | {test} |"
            )

    documented = [r for r in rows if r["usage"]]
    parts.append(
        f"\n## Usage\n\n{len(documented)} script(s) document how to invoke themselves. "
        "The rest take `--help`.\n"
    )
    for row in documented:
        parts.append(f"\n### `{row['path']}`\n")
        parts.append("```")
        parts.append(row["usage"])
        parts.append("```")

    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build the script rows, render the reference page, and write it if the body changed.

    Returns:
        The exit code from `finish_generator` (0 on success, non-zero on a write failure).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--scripts", type=Path, default=SCRIPTS)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.scripts)
    return finish_generator(
        "gen_reference_scripts", args.out, rows, render_markdown, "script"
    )


if __name__ == "__main__":
    raise SystemExit(main())
