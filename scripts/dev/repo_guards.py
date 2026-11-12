#!/usr/bin/env python3
"""Run the repo-wide guards that no test beside a change reaches, once before opening a PR.

A new file trips these guards wherever it lands, and each red in CI costs a full round trip.
A fan-out brief and a hand-run session both call this script, so the list has one home.

    uv run python scripts/dev/repo_guards.py                    # takes one to two minutes
    uv run python scripts/dev/repo_guards.py --drop-candidates  # what the list could lose

The whole suite is out of scope. It costs about five times this set, and the operator ruled it
out for fan-out batches (`.claude/hooks/block-batch-full-suite.py`).

`--drop-candidates` reads the CI failure survey (`ci_failures.py`) over the window, so it needs
`gh` and the sandbox off.

Exit codes: pytest's own when running the guards; from `--drop-candidates`, 0 the proposal
printed and 1 a `gh` call failed; and 64 for bad arguments.
"""

import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import ci_failures

# `lib` is a sibling directory under `scripts/`; a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK
from lib.repo_paths import REPO

DEFAULT_WEEKS = 8


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
    # The first set (#5077).
    "ansible/tests/repo",
    "scripts/docs/tests",
    # The census and structure tests behind 15 failed runs in the week to 2026-11-06 (#7438).
    "scripts/lib/tests/test_entry_points_answer_help.py",
    "scripts/lib/tests/test_script_classify_package_imports.py",
    "scripts/tests/test_script_bootstraps_present.py",
    "scripts/validate/tests/test_validate_shell_templates.py",
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


Run = Callable[..., subprocess.CompletedProcess]


def run_guards(run: Run = subprocess.run) -> int:
    """Run every guard under pytest from the repo root and return pytest's exit code."""
    argv = [sys.executable, "-m", "pytest", "-q", "-n", "auto", *GUARDS]
    return run(argv, cwd=REPO, check=False).returncode


Survey = Callable[[date, date], tuple[set[str], int]]


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
    args = parser.parse_args(argv)
    if not args.drop_candidates:
        return run_guards(run)
    today = datetime.now(UTC).date()
    since = today - timedelta(weeks=args.weeks)
    try:
        failing, unread = survey(since, today)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"gh failed: {exc.stderr or exc}", file=sys.stderr)
        return FAILED
    if unread:
        print(
            f"{unread} red attempts since {since} had no readable log, so a guard "
            "named below may have failed in one of them"
        )
    candidates = drop_candidates(guards, hook_files(), failing)
    for path in candidates:
        print(f"{path}: no CI failure since {since}")
    if not candidates:
        print(f"every guard failed in CI since {since}, or a hook runs it")
    return OK


if __name__ == "__main__":
    sys.exit(main())
