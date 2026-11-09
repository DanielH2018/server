#!/usr/bin/env python3
"""A setup role's own `tests/` reaches no host .

A role's own `tests/*.py` must not fall through to the ROLE-level reach: `tests/` is not a
shipped directory, and with no playbook gate the role-level reach is all three hosts, so
`remaining_setup_hosts_note`'s union over a PR's files would widen the answer for a gated
`templates/` file back out to every host. `initial_setup` has no playbook gate, and its drift
check's script is shipped by a task under `when: inventory_hostname in setup_drift_check_hosts`,
so `setup_file_hosts` reaches daniel-box and daniel-server for it and not daniel-pi.

Each narrowing has a reject half. The reject here is `tasks/crons.yml`: it holds an ungated copy
beside the gated ones, so a change to it must still name all three hosts, and this file pins the
answer so a later narrowing of `tasks/` cannot silence it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_role_tests.py
"""

from deploy_tools.land_lib import land_reach
from deploy_tools.land_lib import land_tags

from lib.repo_paths import REPO as REPO_ROOT

_ROLE = "initial_setup"
_ROLE_DIR = "ansible/roles/setup/initial_setup/"
_SHIPPED = _ROLE_DIR + "templates/setup-drift-check.sh.j2"
_TESTS = _ROLE_DIR + "tests/test_worktree_holders.py"
_UNGATED_TASKS = _ROLE_DIR + "tasks/crons.yml"
_DOC = _ROLE_DIR + "CLAUDE.md"
_ALL_HOSTS = frozenset({"daniel-box", "daniel-server", "daniel-pi"})
_PR_SETUP_PATHS = [_DOC, _SHIPPED, _TESTS]


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_SHIPPED, _TESTS, _UNGATED_TASKS, _DOC)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    crons = (REPO_ROOT / _UNGATED_TASKS).read_text()
    assert "when: inventory_hostname in setup_drift_check_hosts" in crons, (
        "the gate this file pins moved"
    )


def test_the_predicate_matches_land_tags_own():
    """Inlined in land_reach because land_tags imports it; keep the two in step."""
    assert land_tags.is_role_test_path(_TESTS)
    assert not land_tags.is_role_test_path(_SHIPPED)
    assert not land_tags.is_role_test_path(_UNGATED_TASKS)


def test_a_role_test_file_reaches_no_host():
    """The accept half: pytest guards are staged by nothing, so no host runs an old copy."""
    assert land_reach.setup_file_hosts(_ROLE, _TESTS) == frozenset()


def test_a_shipped_file_still_reaches_the_gated_hosts_only():
    """The task's `when:` gate is followed."""
    assert land_reach.setup_file_hosts(_ROLE, _SHIPPED) == frozenset(
        {"daniel-box", "daniel-server"}
    )


def test_a_tasks_file_is_not_dropped_as_a_test_path():
    """The reject half: `tasks/` is NOT dropped (`is_role_test_path`'s docstring says why), and
    the file holding an ungated task keeps naming every host."""
    assert land_reach.setup_file_hosts(_ROLE, _UNGATED_TASKS) == _ALL_HOSTS


def test_a_pr_with_a_test_and_a_doc_owes_nothing_beyond_the_gated_hosts():
    """The union over the PR's files stays at the template's reach, so the Pi is not named."""
    note = land_reach.remaining_setup_hosts_note(_PR_SETUP_PATHS, "daniel-box")
    assert "reaches daniel-server" in note
    assert "daniel-pi" not in note


def test_a_pr_with_only_a_test_and_a_doc_owes_no_host():
    """The verdict is `settled`, not `needs-manual-apply`."""
    assert land_reach.remaining_setup_hosts_note([_DOC, _TESTS], "daniel-box") == ""


def test_a_pr_from_the_pi_still_names_the_gated_hosts():
    """Relative to the host the tick ran on: the same files DO reach daniel-box and
    daniel-server, so a tick that ran on the Pi still owes both -- the narrowing is per
    file, not a blanket silence."""
    note = land_reach.remaining_setup_hosts_note(_PR_SETUP_PATHS, "daniel-pi")
    assert "reaches daniel-box" in note
    assert "reaches daniel-server" in note
