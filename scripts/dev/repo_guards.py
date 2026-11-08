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

# `--drop-candidates` asks the CI failure survey which of these failed within its window.
GUARDS = (
    # The first set (#5077).
    "ansible/tests/repo",
    "scripts/docs/tests",
    "scripts/tests/test_census_rows_test_modules.py",
    "scripts/tests/test_census_rows_test_renders.py",
    "scripts/lib/tests/test_exit_codes.py",
    # The files the `static-ratchet-tests-*` prek hooks run (#5481).
    "scripts/tests/test_scripts_import_direction.py",
    "scripts/tests/test_prek_files_cover_entry_imports.py",
    "ansible/tests/setup/test_lander_deadlines_cover_the_soak.py",
    "ansible/tests/k8s/test_checksum_annotations_documented.py",
    "ansible/tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py",
    "ansible/tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py",
    "evals/tests/test_eval_cases.py",
    "ansible/tests/setup/test_k8s_host_files_pairs.py",
    "ansible/tests/deploy/test_landing_annotations.py",
    "ansible/tests/deploy/test_gitops_deploy_imports.py",
    "scripts/validate/tests/test_validate_alert_rules.py::test_the_tree_rules_meet_every_convention_is_clean",
    "scripts/validate/tests/test_validate_unit_templates.py::test_discover_templates_finds_the_known_set",
    # The census and structure tests behind 15 failed runs in the week to 2026-11-06 (#7438).
    "scripts/lib/tests/test_entry_points_answer_help.py",
    "scripts/lib/tests/test_script_classify_package_imports.py",
    "scripts/tests/test_script_bootstraps_present.py",
    "scripts/validate/tests/test_validate_shell_templates.py",
    "hosts/lib/tests/test_k3s_restart_window.py",
    "scripts/renderer/tests/test_renderer_task_inputs.py",
    "ansible/tests/setup/test_setup_crons_write_a_textfile.py",
    "scripts/deploy_tools/tests/test_land_imports.py",
    "scripts/deploy_tools/tests/test_land_deploy_arm_retired.py",
)


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


Survey = Callable[[date, date], set[str]]


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
        failing = survey(since, today)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"gh failed: {exc.stderr or exc}", file=sys.stderr)
        return FAILED
    candidates = drop_candidates(guards, hook_files(), failing)
    for path in candidates:
        print(f"{path}: no CI failure since {since}")
    if not candidates:
        print(f"every guard failed in CI since {since}, or a hook runs it")
    return OK


if __name__ == "__main__":
    sys.exit(main())
