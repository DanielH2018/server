#!/usr/bin/env python3
"""A role's own `tests/` reaches no cluster, from mapper to verdict .

WHAT THIS DROPS. A role's pytest guards under a tag-less role must not make `shared_roles` name
the role, which would end the landing `needs-manual-apply` with a full `ansible/deploy.yml` as
the remedy. A role's `tests/` covers its `files/*.py` and is staged by nothing
(the `no-role-ships-a-test-file` row of `ansible/tests/repo/test_census_rows_roles.py` holds
that tree-wide), so no deploy can
apply it. Same class as the `.md` rule.

WHAT THIS KEEPS, and why the reject halves below are `tasks/` cases. Dropping a shared role's
`tasks/` as well is wrong for three reasons: a tasks-only PR adding an unregistered role must
still be reported (`test_land_classify.py:110`); a helper's tasks apply live state per caller,
so one caller deployed is not the change applied (`test_land_tags_caller_coverage.py:76`); and
`_supplies_manifest_bytes` names `image-builder/templates/build-job.yaml.j2` as a byte supplier,
which puts that role's own example in the REPORTED set.

THE VERDICT IS ASSERTED for the tests-only half, not just `plane_note`, for the reason
`test_land_doc_change_reaches_a_verdict.py` asserts it: `no_tag_outcome` reads `plane_note`
together with `self_applied`, and only their combination decides between `nothing-to-deploy`
(exit 0) and `needs-manual-apply` (exit 1).

Run: uv run pytest scripts/deploy_tools/tests/test_land_role_tests_reach_no_cluster.py
"""

import pytest


import land_tags
from _land_fakes import MERGE_SHA
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

from lib.repo_paths import REPO as REPO_ROOT

_ROLE_TESTS = "ansible/roles/k8s/arr-notification/tests/test_seed_arr_notification.py"
_ROLE_FILES = "ansible/roles/k8s/arr-notification/files/seed_arr_notification.py"
_ROLE_TASKS = "ansible/roles/k8s/arr-notification/tasks/main.yml"
_BUILD_TASKS = "ansible/roles/k8s/image-builder/tasks/main.yml"


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_ROLE_TESTS, _ROLE_FILES, _ROLE_TASKS, _BUILD_TASKS)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    declared = land_tags.declared_tags()
    still_shared = {"arr-notification", "image-builder"} - declared
    assert still_shared == {"arr-notification", "image-builder"}, (
        f"gained a containers_list entry, so no longer a shared role: {declared & still_shared}"
    )


def test_a_role_test_file_is_clean():
    """The accept half: a role's pytest guards are staged by nothing."""
    assert land_tags.role_for(_ROLE_TESTS) == "arr-notification"
    assert land_tags.is_role_test_path(_ROLE_TESTS) is True
    assert land_tags.shared_roles([_ROLE_TESTS]) == []
    assert land_tags.plane_note([_ROLE_TESTS]) == ""
    assert land_tags.shared_caller_tags([_ROLE_TESTS]) == {}


def test_a_role_task_file_is_flagged():
    """The reject half: tasks/ still owes a deploy, which is a deploy of both callers rather
    than a hand."""
    assert land_tags.is_role_test_path(_ROLE_TASKS) is False
    assert land_tags.shared_roles([_ROLE_TASKS]) == ["arr-notification"]
    assert land_tags.shared_caller_tags([_ROLE_TASKS]) == {
        "arr-notification": {"radarr", "sonarr"}
    }


def test_a_shared_build_role_task_file_is_flagged():
    """image-builder ships templates/, so it supplies applied bytes."""
    assert land_tags.shared_roles([_BUILD_TASKS]) == ["image-builder"]


def test_a_deleted_shared_role_owes_nothing(tmp_path):
    """A role whose directory the change removed has nothing left for any play to run (#3387)."""
    retired = "ansible/roles/k8s/retired-helper/templates/pvc.yaml.j2"
    assert land_tags.shared_roles([retired], declared=set(), roles_root=tmp_path) == []


def test_a_shared_role_still_in_the_tree_is_flagged(tmp_path):
    """The reject half: the same path with its directory present is still reported."""
    (tmp_path / "k8s" / "retired-helper").mkdir(parents=True)
    retired = "ansible/roles/k8s/retired-helper/templates/pvc.yaml.j2"
    assert land_tags.shared_roles([retired], declared=set(), roles_root=tmp_path) == [
        "retired-helper"
    ]


def test_a_role_shipped_file_is_flagged():
    """One directory over from tests/: `files/` is embedded by a caller's manifest."""
    assert land_tags.shared_roles([_ROLE_FILES]) == ["arr-notification"]


def test_a_test_file_beside_a_real_change_does_not_excuse_it():
    """One quiet tests/ edit must not take the code change it covers with it."""
    reached = land_tags.shared_caller_tags([_ROLE_TESTS, _ROLE_FILES])
    assert reached == {"arr-notification": {"radarr", "sonarr"}}


def _verdict(landing, files: list[str]) -> Outcome:
    """The verdict `land.sh` reaches for a PR with this file list and no service tag."""
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    quiet = land_tags.quiet_paths(files, "")
    ln.plane = land_tags.plane_note(files, quiet=quiet)
    ln.self_applied = land_tags.self_applied(files, quiet=quiet)
    ln.self_applied_command = land_tags.self_applied_command(files, quiet=quiet)
    ln.remaining_setup = ""
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    return exc.value


def test_a_role_test_change_ends_nothing_to_deploy(landing):
    outcome = _verdict(landing, [_ROLE_TESTS])
    assert (outcome.verdict, outcome.rc) == ("nothing-to-deploy", 0)


def test_a_role_task_change_owes_no_hand_because_its_callers_deploy():
    """The tag path decides it: the landing deploys sonarr and radarr."""
    assert land_tags.plane_note([_ROLE_TASKS]) == ""


# The declared-role half. A DECLARED role's tests/ must not derive that role's deploy tag,
# or a pytest-only PR to monitor-bridge deploys monitor-bridge.
_DECLARED_TESTS = "ansible/roles/k8s/monitor-bridge/tests/test_b2_storage.py"
_DECLARED_FILES = "ansible/roles/k8s/monitor-bridge/files/checks/service.py"


def test_the_declared_paths_under_test_still_exist():
    """Non-vacuity for the declared half: the role must keep its containers_list entry."""
    missing = [
        p for p in (_DECLARED_TESTS, _DECLARED_FILES) if not (REPO_ROOT / p).exists()
    ]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    assert "monitor-bridge" in land_tags.declared_tags(), (
        "monitor-bridge lost its containers_list entry, so the declared-role cases below "
        "test the shared-role path instead"
    )


def test_a_declared_role_test_file_derives_no_tag():
    """The accept half: no tag, so no rollout, restart window or health gate."""
    assert land_tags.tag_for(_DECLARED_TESTS) is None
    assert land_tags.derive([_DECLARED_TESTS], 1) == land_tags.Derivation(
        [], land_tags.DeriveSource.PR
    )


def test_a_declared_role_shipped_file_still_derives_its_tag():
    """The reject half: one directory over from tests/, the tag is still owed."""
    assert land_tags.tag_for(_DECLARED_FILES) == "monitor-bridge"
    assert land_tags.derive([_DECLARED_FILES], 1).tags == ["monitor-bridge"]


def test_a_declared_role_test_file_beside_a_real_change_does_not_excuse_it():
    assert land_tags.derive([_DECLARED_TESTS, _DECLARED_FILES], 2).tags == [
        "monitor-bridge"
    ]


def test_a_declared_role_test_change_ends_nothing_to_deploy(landing):
    outcome = _verdict(landing, [_DECLARED_TESTS])
    assert (outcome.verdict, outcome.rc) == ("nothing-to-deploy", 0)
