#!/usr/bin/env python3
"""A role's own `tests/` reaches no cluster, from mapper to verdict -- issue #1729, in part.

WHAT THIS DROPS. `shared_roles` named a role for ANY path under it, so a PR whose only change
under a tag-less role was that role's pytest guards ended `needs-manual-apply` and printed a
full `ansible/deploy.yml` as the remedy. A role's `tests/` covers its `files/*.py` and is staged
by nothing (`ansible/tests/repo/test_no_role_ships_a_test_file.py` holds that tree-wide), so no
deploy can apply it. Same class as the `.md` rule (#1701).

WHAT THIS KEEPS, and why the reject halves below are `tasks/` cases. Issue #1729 proposed
dropping a shared role's `tasks/` as well, and three facts refute that: a tasks-only PR adding
an unregistered role must still be reported (`test_land_classify.py:110`, #1544); a helper's
tasks apply live state per caller, so one caller deployed is not the change applied
(`test_land_tags_caller_coverage.py:76`, #1397); and `_supplies_manifest_bytes`, the predicate
#1729 cites as precedent, names `volume-claim/templates/pvc.yaml.j2` as a byte supplier, which
puts that role's own example in the REPORTED set.

THE VERDICT IS ASSERTED, not just `plane_note`, for the reason
`test_land_doc_change_reaches_a_verdict.py` asserts it: `no_tag_outcome` reads `plane_note`
together with `self_applied`, and only their combination decides between `nothing-to-deploy`
(exit 0) and `needs-manual-apply` (exit 1).

Run: uv run pytest scripts/deploy_tools/tests/test_land_role_tests_reach_no_cluster.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_tags
from _land_fakes import MERGE_SHA
from deploy_tools.land_lib import deploy
from deploy_tools.land_lib.outcome import Outcome

REPO_ROOT = Path(__file__).resolve().parents[3]

_ROLE_TESTS = "ansible/roles/k8s/arr-notification/tests/test_seed_arr_notification.py"
_ROLE_FILES = "ansible/roles/k8s/arr-notification/files/seed_arr_notification.py"
_ROLE_TASKS = "ansible/roles/k8s/arr-notification/tasks/main.yml"
_CLAIM_TASKS = "ansible/roles/k8s/volume-claim/tasks/claim.yml"


def test_the_paths_under_test_still_exist():
    """Non-vacuity: every case below reads green over a renamed file or role."""
    named = (_ROLE_TESTS, _ROLE_FILES, _ROLE_TASKS, _CLAIM_TASKS)
    missing = [p for p in named if not (REPO_ROOT / p).exists()]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    declared = land_tags.declared_tags()
    still_shared = {"arr-notification", "volume-claim"} - declared
    assert still_shared == {"arr-notification", "volume-claim"}, (
        f"gained a containers_list entry, so no longer a shared role: {declared & still_shared}"
    )


def test_a_role_test_file_is_clean():
    """The accept half: a role's pytest guards are staged by nothing."""
    assert land_tags.role_for(_ROLE_TESTS) == "arr-notification"
    assert land_tags.is_role_test_path(_ROLE_TESTS) is True
    assert land_tags.shared_roles([_ROLE_TESTS]) == []
    assert land_tags.plane_note([_ROLE_TESTS]) == ""


def test_a_role_task_file_is_flagged():
    """The reject half, and the half of #1729 that is refuted: tasks/ still owes a hand."""
    assert land_tags.is_role_test_path(_ROLE_TASKS) is False
    assert land_tags.shared_roles([_ROLE_TASKS]) == ["arr-notification"]
    assert "arr-notification" in land_tags.plane_note([_ROLE_TASKS])


def test_a_shared_storage_role_task_file_is_flagged():
    """#1729's own example. volume-claim ships templates/, so it supplies applied bytes."""
    assert land_tags.shared_roles([_CLAIM_TASKS]) == ["volume-claim"]
    assert "volume-claim" in land_tags.plane_note([_CLAIM_TASKS])


def test_a_role_shipped_file_is_flagged():
    """One directory over from tests/: `files/` is embedded by a caller's manifest."""
    assert land_tags.shared_roles([_ROLE_FILES]) == ["arr-notification"]


def test_a_test_file_beside_a_real_change_does_not_excuse_it():
    """One quiet tests/ edit must not take the code change it covers with it."""
    assert "arr-notification" in land_tags.plane_note([_ROLE_TESTS, _ROLE_FILES])


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


def test_a_role_task_change_still_ends_needs_manual_apply(landing):
    outcome = _verdict(landing, [_ROLE_TASKS])
    assert (outcome.verdict, outcome.rc) == ("needs-manual-apply", 1)
