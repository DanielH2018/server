#!/usr/bin/env python3
"""Run the repo-wide guards that no test beside a change reaches, once before opening a PR.

A new file trips these guards wherever it lands, and each red in CI costs a full round trip.
A fan-out brief and a hand-run session both call this script, so the list has one home.

    uv run python scripts/dev/repo_guards.py                    # takes one to two minutes
    uv run python scripts/dev/repo_guards.py --drop-candidates  # what the list could lose

The whole suite is out of scope. It costs about five times this set, and the operator ruled it
out for fan-out batches (`.claude/hooks/block-batch-full-suite.py`).

Exit codes: pytest's own when running the guards, 0 from `--drop-candidates`, and 64 for bad
arguments.
"""

import subprocess
import sys
import tomllib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# `lib` is a sibling directory under `scripts/`; a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser
from lib.repo_paths import REPO

DEFAULT_WEEKS = 8

# Each guard maps to the date of the last survey that counted a CI failure in it. A later
# failure moves its date forward. `--drop-candidates` reads these dates, so a stale one only
# proposes a drop for a person to check.
GUARDS = {
    # The first set (#5077).
    "ansible/tests/repo": "2026-10-17",
    "scripts/docs/tests": "2026-10-17",
    "scripts/tests/test_census_rows_test_modules.py": "2026-10-17",
    "scripts/tests/test_census_rows_test_renders.py": "2026-10-17",
    "scripts/lib/tests/test_exit_codes.py": "2026-10-17",
    # The files the `static-ratchet-tests-*` prek hooks run (#5481).
    "scripts/tests/test_scripts_import_direction.py": "2026-10-17",
    "scripts/tests/test_prek_files_cover_entry_imports.py": "2026-10-17",
    "ansible/tests/setup/test_lander_deadlines_cover_the_soak.py": "2026-10-17",
    "ansible/tests/k8s/test_checksum_annotations_documented.py": "2026-10-17",
    "ansible/tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py": "2026-10-17",
    "ansible/tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py": "2026-10-17",
    "evals/tests/test_eval_cases.py": "2026-10-25",
    "ansible/tests/setup/test_k8s_host_files_pairs.py": "2026-10-25",
    "ansible/tests/deploy/test_landing_annotations.py": "2026-10-25",
    "ansible/tests/deploy/test_gitops_deploy_imports.py": "2026-10-25",
    "scripts/validate/tests/test_validate_alert_rules.py::test_the_tree_rules_meet_every_convention_is_clean": "2026-10-25",
    "scripts/validate/tests/test_validate_unit_templates.py::test_discover_templates_finds_the_known_set": "2026-10-25",
    # The census and structure tests behind 15 failed runs in the week to 2026-11-06 (#7438).
    "scripts/lib/tests/test_entry_points_answer_help.py": "2026-11-06",
    "scripts/lib/tests/test_script_classify_package_imports.py": "2026-11-06",
    "scripts/tests/test_script_bootstraps_present.py": "2026-11-06",
    "scripts/validate/tests/test_validate_shell_templates.py": "2026-11-06",
    "hosts/lib/tests/test_k3s_restart_window.py": "2026-11-06",
    "scripts/renderer/tests/test_renderer_task_inputs.py": "2026-11-06",
    "ansible/tests/setup/test_setup_crons_write_a_textfile.py": "2026-11-06",
    "scripts/deploy_tools/tests/test_land_imports.py": "2026-11-06",
    "scripts/renderer/tests/test_renderer_r4_precondition.py": "2026-11-06",
    "scripts/deploy_tools/tests/test_land_deploy_arm_retired.py": "2026-11-06",
}


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


def drop_candidates(
    guards: dict[str, str], hooks: list[str], today: date, weeks: int
) -> list[str]:
    """The guards with no counted failure in `weeks` weeks.

    A guard that holds a hook's file stays: a hook file passes at commit, so CI rarely reports
    it, and `test_repo_guards.py` requires this list to cover every hook file.
    """
    cutoff = today - timedelta(weeks=weeks)
    return [
        path
        for path, last_seen in guards.items()
        if date.fromisoformat(last_seen) < cutoff
        and not any(hook.startswith(path) for hook in hooks)
    ]


Run = Callable[..., subprocess.CompletedProcess]


def run_guards(run: Run = subprocess.run) -> int:
    """Run every guard under pytest from the repo root and return pytest's exit code."""
    argv = [sys.executable, "-m", "pytest", "-q", "-n", "auto", *GUARDS]
    return run(argv, cwd=REPO, check=False).returncode


def main(
    argv: list[str] | None = None,
    guards: dict[str, str] = GUARDS,
    run: Run = subprocess.run,
) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-candidates",
        action="store_true",
        help="print the guards with no counted failure in --weeks weeks, run nothing",
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
    candidates = drop_candidates(guards, hook_files(), today, args.weeks)
    for path in candidates:
        print(f"{path}: last counted failure {guards[path]}")
    if not candidates:
        print(f"no guard is older than {args.weeks} weeks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
