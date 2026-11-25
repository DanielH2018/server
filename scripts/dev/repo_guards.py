#!/usr/bin/env python3
"""Run the repo-wide guards that no test beside a change reaches, once before opening a PR.

A new file trips these guards wherever it lands, and each red in CI costs a full round trip.
A fan-out brief and a hand-run session both call this script, so the list has one home.

    uv run python scripts/dev/repo_guards.py                    # takes one to two minutes
    uv run python scripts/dev/repo_guards.py --drop-candidates  # what the list could lose
    uv run python scripts/dev/repo_guards.py --drop-candidates --cited  # what tests/ could lose

After the guards, the run prints advice for the branch: each instruction-file section that
cites code the branch changed and whose prose it left unchanged, each name the branch
removed that a doc or a comment still uses, and a count of what the branch adds with each
new name that nothing outside tests calls and each new function that takes or returns a raw
dict. Advice changes no exit code.

The whole suite is out of scope. It costs about five times this set, and the operator ruled it
out for fan-out batches (`.claude/hooks/block-batch-full-suite.py`).

`--drop-candidates` reads the CI failure survey (`ci_failures.py`) over the window, so it needs
`gh` and the sandbox off. With `--cited` it lists, in place of the guards, each check file under
`tests/` whose text cites an issue (`#1234`) or a date (`2026-11-18`) and that no red attempt on
master named. A red attempt on a PR branch does not count there. Either list proposes and
deletes nothing: a check that never fails can still be load-bearing, so the operator decides
which go. GitHub keeps an Actions log for 90 days by default, so a longer window reads most red
attempts as unreadable.

Exit codes: pytest's own when running the guards; from `--drop-candidates`, 0 the proposal
printed and 1 a `gh` call failed; and 64 for bad arguments.
"""

import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dev import ci_failures


from lib.branch_adds import added_advice
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.facts.lint import vanished_advice
from lib.facts.notice import branch_notice
from lib.repo_paths import REPO

DEFAULT_WEEKS = 8
CITED_BRANCH = "master"
CITATION = re.compile(r"(?<![\w/&])#\d{2,}\b|\b20\d\d-\d\d-\d\d\b")


def hook_files(repo: Path = REPO) -> list[str]:
    """The test files and nodes the `static-ratchet-tests*` prek hooks run at commit time."""
    hooks = [
        hook
        for section in tomllib.loads((repo / "prek.toml").read_text())["repos"]
        for hook in section.get("hooks", [])
    ]
    entries = [h["entry"] for h in hooks if h["id"].startswith("static-ratchet-tests")]
    words = (word for entry in entries for word in entry.split())
    return [word for word in words if word.split("::", 1)[0].endswith(".py")]


def covers(guard: str, module: str) -> bool:
    """Whether a failing test module falls under the guard's file, node or directory."""
    path = guard.split("::", 1)[0].rstrip("/")
    return module == path or module.startswith(f"{path}/")


# The guards no hook runs; `guards()` adds every hook file a listed directory does not hold.
LISTED = (
    # The first set.
    "tests/repo",
    "scripts/docs/tests",
    "scripts/lib/tests/test_entry_points_answer_help.py",
    "scripts/lib/tests/test_script_classify_package_imports.py",
    "scripts/validate/tests/test_render_shell.py",
    "hosts/lib/tests/test_k3s_restart_window.py",
    "scripts/renderer/tests/test_renderer_task_inputs.py",
    "scripts/deploy_tools/tests/test_land_imports.py",
)


def guards(repo: Path = REPO) -> tuple[str, ...]:
    """`LISTED` plus each file the ratchet hooks run that no listed directory holds."""
    hooks = [f for f in hook_files(repo) if not any(covers(g, f) for g in LISTED)]
    return LISTED + tuple(dict.fromkeys(hooks))


# `--drop-candidates` asks the CI failure survey which of these failed within its window.
GUARDS = guards()


def drop_candidates(
    guards: Iterable[str], hooks: list[str], failing: set[str]
) -> list[str]:
    """The guards that hold none of the `failing` test modules.

    A guard that holds a hook's file stays: a hook file passes at commit, so CI rarely reports
    it, and `test_repo_guards.py` requires this list to cover every hook file.
    """
    return [
        guard
        for guard in guards
        if not any(covers(guard, module) for module in failing)
        and not any(hook.startswith(guard) for hook in hooks)
    ]


def citing_checks(repo: Path = REPO) -> dict[str, list[str]]:
    """Map each check file under `tests/` that cites an issue or a date to what it cites."""
    found: dict[str, list[str]] = {}
    for path in sorted((repo / "tests").rglob("test_*.py")):
        cited = list(dict.fromkeys(CITATION.findall(path.read_text())))
        if cited:
            found[path.relative_to(repo).as_posix()] = cited
    return found


Run = Callable[..., subprocess.CompletedProcess]

NOTICE_REF = "origin/master"

Source = Callable[[Path], str | None]


def _reread(repo: Path) -> str | None:
    return branch_notice(repo, NOTICE_REF, fmt="reread")


def _removed_names(repo: Path) -> str | None:
    return vanished_advice(repo, NOTICE_REF)


def _branch_adds(repo: Path) -> str | None:
    return added_advice(repo, NOTICE_REF)


ADVICE: tuple[tuple[str, Source], ...] = (
    ("facts notice", _reread),
    ("removed names", _removed_names),
    ("branch adds", _branch_adds),
)


def advice(repo: Path = REPO, sources: tuple[tuple[str, Source], ...] = ADVICE) -> str:
    """The lines of every source of ``sources``, or one line saying why a source gave none.

    A source returns its lines, an empty string when it has nothing to say, or None when the
    branch has no merge base with ``NOTICE_REF``.
    """
    blocks = []
    for name, source in sources:
        try:
            text = source(repo)
        except Exception as exc:  # advice never fails the run, so its crash is one line
            text = f"{name} skipped: {type(exc).__name__}: {exc}"
        if text is None:
            text = f"{name} skipped: no merge base with {NOTICE_REF}; is it fetched?"
        if text:
            blocks.append(text)
    return "\n".join(blocks)


def run_guards(run: Run = subprocess.run, advice: Callable[[], str] = advice) -> int:
    """Run every guard under pytest from the repo root and return pytest's exit code.

    The lines of ``advice`` follow pytest's output and never change the exit code.
    """
    argv = [sys.executable, "-m", "pytest", "-q", "-n", "auto", *GUARDS]
    code = run(argv, cwd=REPO, check=False).returncode
    text = advice()
    if text:
        print(text)
    return code


Survey = Callable[[date, date, str | None], tuple[set[str], int]]


def main(
    argv: list[str] | None = None,
    guards: Iterable[str] = GUARDS,
    run: Run = subprocess.run,
    survey: Survey = ci_failures.failing_test_modules,
) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-candidates",
        action="store_true",
        help="print the guards with no CI failure in --weeks weeks, run nothing",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_WEEKS,
        help=f"the window for --drop-candidates (default {DEFAULT_WEEKS})",
    )
    parser.add_argument(
        "--cited",
        action="store_true",
        help="with --drop-candidates, list the tests/ files that cite an issue or a date"
        f" and have no {CITED_BRANCH} failure, in place of the guards",
    )
    args = parser.parse_args(argv)
    if args.cited and not args.drop_candidates:
        parser.error("--cited needs --drop-candidates")
    if not args.drop_candidates:
        return run_guards(run)
    today = datetime.now(UTC).date()
    since = today - timedelta(weeks=args.weeks)
    branch = CITED_BRANCH if args.cited else None
    try:
        failing, unread = survey(since, today, branch)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"gh failed: {exc.stderr or exc}", file=sys.stderr)
        return FAILED
    if unread:
        print(
            f"{unread} red attempts since {since} had no readable log, so a file "
            "named below may have failed in one of them"
        )
    if args.cited:
        cited = citing_checks()
        quiet = {path: refs for path, refs in cited.items() if path not in failing}
        print(
            f"{len(quiet)} of {len(cited)} check files that cite an issue or a date have"
            f" no {CITED_BRANCH} failure since {since}; the operator decides which go"
        )
        for path, refs in quiet.items():
            print(f"{path}  {', '.join(refs)}")
        return OK
    candidates = drop_candidates(guards, hook_files(), failing)
    for path in candidates:
        print(f"{path}: no CI failure since {since}")
    if not candidates:
        print(f"every guard failed in CI since {since}, or a hook runs it")
    return OK


if __name__ == "__main__":
    sys.exit(main())
