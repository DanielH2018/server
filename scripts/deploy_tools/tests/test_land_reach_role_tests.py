#!/usr/bin/env python3
"""A setup role's own `tests/` reaches no host -- issue #1885.

WHAT WENT WRONG. Landing PR #1884 ended `needs-manual-apply`, prescribing
`initial_setup.yml --tags gitops_deploy` on daniel-server and daniel-pi. Nothing there needed
applying: the role dispatches on `has_gitops` inside `tasks/main.yml`, `setup_file_hosts`
already follows that `include_tasks` gate, and every `files/*.py` the PR touched read as
daniel-box only. The three `tests/*.py` beside them did not: `tests/` is not a shipped
directory, so each fell through to the ROLE-level reach (all three hosts, the role has no
playbook gate), and `remaining_setup_hosts_note`'s union over the PR's files widened the
box-only answer back out. The issue's stated cause -- that the include gate was not
followed -- was already fixed by the time it was filed; the tests/ fall-through was the bug.

Each narrowing has a reject half. The reject here is `tasks/teardown.yml`: a change to the
half of the dispatcher that runs where `has_gitops` is false must still name daniel-server
and daniel-pi, and it does so through the "unknown stays wide" fallback, which this file
pins so a later narrowing of `tasks/` cannot silence it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_role_tests.py
"""

from pathlib import Path


import land_reach
import land_tags

REPO_ROOT = Path(__file__).resolve().parents[3]

_ROLE = "gitops_deploy"
_FILES = "ansible/roles/setup/gitops_deploy/files/deploy_state.py"
_TESTS = "ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py"
_TEARDOWN = "ansible/roles/setup/gitops_deploy/tasks/teardown.yml"
# PR #1884's file list, verbatim (`gh pr view 1884 --json files`), minus the paths outside
# the setup plane -- those derive tags or nothing and never reach this note.
_PR_1884_SETUP_PATHS = [
    "ansible/roles/setup/gitops_deploy/CLAUDE.md",
    "ansible/roles/setup/gitops_deploy/files/deploy_defer.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_git.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_handlers.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_locks.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_remediation.py",
    "ansible/roles/setup/gitops_deploy/files/deploy_state.py",
    "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
    "ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_alert_channels.py",
    "ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_lock_contention.py",
]


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_FILES, _TESTS, _TEARDOWN, *_PR_1884_SETUP_PATHS)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    main = (REPO_ROOT / "ansible/roles/setup/gitops_deploy/tasks/main.yml").read_text()
    assert "include_tasks: teardown.yml" in main, (
        "the dispatcher shape this file pins moved"
    )


def test_the_predicate_matches_land_tags_own():
    """Inlined in land_reach because land_tags imports it; keep the two in step."""
    assert land_tags.is_role_test_path(_TESTS)
    assert not land_tags.is_role_test_path(_FILES)
    assert not land_tags.is_role_test_path(_TEARDOWN)


def test_a_role_test_file_reaches_no_host():
    """The accept half: pytest guards are staged by nothing, so no host runs an old copy."""
    assert land_reach.setup_file_hosts(_ROLE, _TESTS) == frozenset()


def test_a_shipped_file_still_reaches_the_gitops_host_only():
    """The include gate is followed (the issue's remediation was already in place)."""
    assert land_reach.setup_file_hosts(_ROLE, _FILES) == frozenset({"daniel-box"})


def test_the_teardown_half_still_reaches_the_other_hosts():
    """The reject half: `tasks/` is NOT dropped (`is_role_test_path`'s docstring says why),
    and the file that runs where `has_gitops` is false must keep naming those hosts."""
    hosts = land_reach.setup_file_hosts(_ROLE, _TEARDOWN)
    assert {"daniel-server", "daniel-pi"} <= hosts


def test_pr_1884_owes_no_host_beyond_the_tick():
    """The verdict PR #1884 should have read: `settled`, not `needs-manual-apply`."""
    assert (
        land_reach.remaining_setup_hosts_note(_PR_1884_SETUP_PATHS, "daniel-box") == ""
    )


def test_pr_1884_from_another_host_still_names_the_gitops_host():
    """Relative to the host the tick ran on: the same files DO reach daniel-box, so a tick
    that ran elsewhere still owes it -- the narrowing is per file, not a blanket silence."""
    note = land_reach.remaining_setup_hosts_note(_PR_1884_SETUP_PATHS, "daniel-server")
    assert "daniel-box" in note
    assert "daniel-pi" not in note
