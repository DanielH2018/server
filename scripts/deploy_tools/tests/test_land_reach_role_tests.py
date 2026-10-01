#!/usr/bin/env python3
"""A setup role's own `tests/` reaches no host .

A role's own `tests/*.py` must not fall through to the ROLE-level reach: `tests/` is not a
shipped directory, and with no playbook gate the role-level reach is all three hosts, so
`remaining_setup_hosts_note`'s union over a PR's files would widen the box-only answer for the
role's `files/*.py` back out to every host. The role dispatches on `has_gitops` inside
`tasks/main.yml`, and `setup_file_hosts` follows that `include_tasks` gate.

Each narrowing has a reject half. The reject here is `tasks/teardown.yml`: a change to the
half of the dispatcher that runs where `has_gitops` is false must still name daniel-server
and daniel-pi, through the `not has_gitops` gate on the include that pulls it in; this file pins
the answer, so a later narrowing of `tasks/` cannot silence it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_role_tests.py
"""

import land_reach
import land_tags

from lib.repo_paths import REPO as REPO_ROOT

_ROLE = "gitops_deploy"
_FILES = "ansible/roles/setup/gitops_deploy/files/deploy_state.py"
_TESTS = "ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py"
_TEARDOWN = "ansible/roles/setup/gitops_deploy/tasks/teardown.yml"
_INSTALL = "ansible/roles/setup/gitops_deploy/tasks/install.yml"
# A PR's file list, verbatim (`gh pr view <n> --json files`), minus the paths outside the
# setup plane -- those derive tags or nothing and never reach this note.
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
    named = (_FILES, _TESTS, _TEARDOWN, _INSTALL, *_PR_1884_SETUP_PATHS)
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
    """The include gate is followed."""
    assert land_reach.setup_file_hosts(_ROLE, _FILES) == frozenset({"daniel-box"})


def test_the_teardown_half_still_reaches_the_other_hosts():
    """The reject half: `tasks/` is NOT dropped (`is_role_test_path`'s docstring says why),
    and the file that runs where `has_gitops` is false must keep naming those hosts. A
    `tasks/` path reads the include chain above it, so each half of the dispatcher
    names exactly the hosts its `include_tasks` gate admits."""
    assert land_reach.setup_file_hosts(_ROLE, _TEARDOWN) == frozenset(
        {"daniel-server", "daniel-pi"}
    )
    assert land_reach.setup_file_hosts(_ROLE, _INSTALL) == frozenset({"daniel-box"})


def test_pr_1884_owes_no_host_beyond_the_tick():
    """The verdict is `settled`, not `needs-manual-apply`."""
    assert (
        land_reach.remaining_setup_hosts_note(_PR_1884_SETUP_PATHS, "daniel-box") == ""
    )


def test_pr_1884_from_another_host_still_names_the_gitops_host():
    """Relative to the host the tick ran on: the same files DO reach daniel-box, so a tick
    that ran elsewhere still owes it -- the narrowing is per file, not a blanket silence."""
    note = land_reach.remaining_setup_hosts_note(_PR_1884_SETUP_PATHS, "daniel-server")
    assert "daniel-box" in note
    assert "daniel-pi" not in note
