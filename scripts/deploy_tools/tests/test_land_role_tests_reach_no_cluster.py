#!/usr/bin/env python3
"""A role's own `tests/` reaches no cluster, from mapper to verdict.

A DECLARED role's `tests/` must not derive that role's deploy tag, or a pytest-only PR to
monitor-bridge deploys monitor-bridge. A role's `tests/` covers its `files/*.py` and is staged
by nothing (the `no-role-ships-a-pytest-file` row of `ansible/tests/repo/test_census_rows_roles.py`
holds that tree-wide), so no deploy can apply it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_role_tests_reach_no_cluster.py
"""

import pytest

from _land_fakes import MERGE_SHA
from deploy_tools.land_lib import deploy, land_tags
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome
from lib.repo_paths import REPO as REPO_ROOT
from lib.role_paths import tag_for

_DECLARED_TESTS = "ansible/roles/k8s/monitor-bridge/tests/test_b2_storage.py"
_DECLARED_FILES = "ansible/roles/k8s/monitor-bridge/files/checks/service.py"


def test_the_declared_paths_under_test_still_exist():
    """Non-vacuity: the role must keep its containers_list entry."""
    missing = [
        p for p in (_DECLARED_TESTS, _DECLARED_FILES) if not (REPO_ROOT / p).exists()
    ]
    assert not missing, f"paths moved, so these cases check nothing: {missing}"
    assert "monitor-bridge" in land_tags.declared_tags(), (
        "monitor-bridge lost its containers_list entry, so the cases below check nothing"
    )


def test_a_declared_role_test_file_derives_no_tag():
    """The accept half: no tag, so no rollout, restart window or health gate."""
    assert tag_for(_DECLARED_TESTS, land_tags.declared_tags()) is None
    assert land_tags.derive([_DECLARED_TESTS], 1) == land_tags.Derivation(
        [], land_tags.DeriveSource.PR
    )


def test_a_declared_role_shipped_file_still_derives_its_tag():
    """The reject half: one directory over from tests/, the tag is still owed."""
    assert land_tags.derive([_DECLARED_FILES], 1).tags == ["monitor-bridge"]


def test_a_declared_role_test_file_beside_a_real_change_does_not_excuse_it():
    assert land_tags.derive([_DECLARED_TESTS, _DECLARED_FILES], 2).tags == [
        "monitor-bridge"
    ]


def test_a_declared_role_test_change_ends_nothing_to_deploy(landing):
    ln, _ = landing(None)
    ln.merge_sha = MERGE_SHA
    ln.classification = Classification(plane=land_tags.plane_note([_DECLARED_TESTS]))
    with pytest.raises(Outcome) as exc:
        deploy.no_tag_outcome(ln)
    assert (exc.value.verdict, exc.value.rc) == ("nothing-to-deploy", 0)
