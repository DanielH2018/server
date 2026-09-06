#!/usr/bin/env python3
"""A changed helper role is covered by the tags that already ran it -- `covered_roles`.

`test_land_tags.py` owns the split between a deployable role and a shared one; this owns the
question that comes after it. A role under ansible/roles/k8s/ with no `containers_list` entry
has no tag, but `deploy.yml` runs it under the tag of EVERY role whose tasks include it -- so a
landing whose own tags cover all of its callers has applied it, and reporting it as STILL
UNAPPLIED sends an operator at a full `deploy.yml` for nothing (issue #1397).

Split from test_land_tags.py rather than appended to it: that module is at its length cap.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_caller_coverage.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_tags

# The declared set these cases need, passed to each call rather than patched over
# `declared_tags` -- every function here takes it as a parameter, which is the seam. Pinned for
# the reason test_land_tags.py pins its own: a service retirement must not turn a derivation
# test red. The caller map itself is read LIVE, because "who includes arr-notification" is the
# fact under test.
_DECLARED = {"configarr", "jellyfin", "radarr", "sonarr", "tdarr"}

# PR #1393's real 13-path file list, read from `gh pr view 1393 --json files` on 2026-09-06.
# `arr-notification` is a helper with no containers_list entry, and the PR carries BOTH of its
# callers -- so the landing deployed sonarr and radarr, which ran arr-notification's tasks
# under each of their tags, and land.sh still asked for a full deploy.yml (issue #1397).
_PR_1393_FILES = [
    "ansible/roles/k8s/arr-notification/CLAUDE.md",
    "ansible/roles/k8s/arr-notification/defaults/main.yml",
    "ansible/roles/k8s/arr-notification/files/seed_arr_notification.py",
    "ansible/roles/k8s/arr-notification/tasks/main.yml",
    "ansible/roles/k8s/arr-notification/tests/test_seed_arr_notification.py",
    "ansible/roles/k8s/radarr/tasks/main.yml",
    "ansible/roles/k8s/sonarr/tasks/main.yml",
    "ansible/tests/k8s/test_container_security_context.py",
    "docs/assets/generated/fragments/autodeploy-coverage.md",
    "docs/assets/generated/fragments/staging-coverage.md",
    "pyproject.toml",
    "scripts/diagnostics/tests/test_probe_releases.py",
    "scripts/lib/k8s_roles.py",
]


def test_a_helper_role_landed_with_all_its_callers_is_not_reported():
    """The accept half of the caller rule, on the measured file list.

    Both callers are in the tags, so the helper's tasks ran under both -- naming it as STILL
    UNAPPLIED sends an operator at `ansible-playbook ansible/deploy.yml` for work already
    applied, and repeated false alarms are how a real one stops being read.
    """
    tags, source = land_tags.derive(_PR_1393_FILES, len(_PR_1393_FILES), set(_DECLARED))
    assert (source, tags) == ("pr", ["radarr", "sonarr"])
    assert land_tags.shared_roles(_PR_1393_FILES, set(_DECLARED)) == [
        "arr-notification"
    ], "still a role with no containers_list entry -- the note changes, not the split"
    assert land_tags.plane_note(_PR_1393_FILES, set(_DECLARED)) == ""


def test_a_helper_role_landed_without_its_callers_is_still_reported():
    """The reject half by caller coverage: one caller deployed is not the helper applied.

    Deploying sonarr re-runs arr-notification's tasks for sonarr alone. radarr keeps the old
    behaviour, so the change is half-applied and a hand still owes the rest.
    """
    files = [
        "ansible/roles/k8s/arr-notification/tasks/main.yml",
        "ansible/roles/k8s/sonarr/tasks/main.yml",
    ]
    assert land_tags.derive(files, 2, set(_DECLARED)).tags == ["sonarr"]
    assert "arr-notification" in land_tags.plane_note(files, set(_DECLARED))


def test_a_changed_role_with_neither_an_entry_nor_a_caller_is_still_reported():
    """The reject half by census: a role nothing includes can only be applied by hand.

    The suppression keys on the caller map, so a role absent from it must fall through to the
    note exactly as before -- otherwise the fix trades a false alarm for a silence.
    """
    files = ["ansible/roles/k8s/nosuchrole/templates/deployment.yaml.j2"]
    assert land_tags.derive(files, 1, set(_DECLARED)).tags == []
    note = land_tags.plane_note(files, set(_DECLARED))
    assert "nosuchrole" in note
    assert "ansible/deploy.yml" in note
