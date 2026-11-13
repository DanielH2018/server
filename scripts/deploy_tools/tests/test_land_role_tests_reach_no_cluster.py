#!/usr/bin/env python3
"""A role's own `tests/` reaches no cluster, from mapper to verdict.

A DECLARED role's `tests/` must not derive that role's deploy tag, or a pytest-only PR to
monitor-bridge deploys monitor-bridge. A role's `tests/` covers its `files/*.py` and is staged
by nothing (the `no-role-ships-a-pytest-file` row of `tests/repo/test_census_rows_roles.py`
holds that tree-wide), so no deploy can apply it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_role_tests_reach_no_cluster.py
"""

import dataclasses

import pytest

from _land_fakes import MERGE_SHA, Fakes
from deploy_tools.land_lib import classify, land_tags
from deploy_tools.land_lib.outcome import Outcome
from lib.repo_paths import REPO as REPO_ROOT
from lib.role_paths import tag_for

_DECLARED_TESTS = "k8s/roles/monitor-bridge/tests/test_b2_storage.py"
_DECLARED_FILES = "k8s/roles/monitor-bridge/files/checks/service.py"


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


def _classified_landing(landing, paths: list[str]):
    """A landing whose PR changed `paths`, classified by the REAL plane note and derivation."""
    ln, _ = landing(
        Fakes(
            gh_views={
                "files,changedFiles": {
                    "files": [{"path": p} for p in paths],
                    "changedFiles": len(paths),
                }
            },
            declared_at=land_tags.declared_tags(),
        )
    )
    ln.classifier = dataclasses.replace(
        ln.classifier, plane_note=land_tags.plane_note, derive=land_tags.derive
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    return ln


def test_a_declared_role_test_change_ends_nothing_to_deploy(landing):
    ln = _classified_landing(landing, [_DECLARED_TESTS])
    assert ln.resolved_tags == []
    with pytest.raises(Outcome) as exc:
        classify.shortcut_if_nothing(ln)
    assert (exc.value.verdict, exc.value.rc) == ("nothing-to-deploy", 0)


def test_a_declared_role_shipped_file_change_does_not_end_the_landing(landing):
    """The reject half: the same path through the same steps owes its tag."""
    ln = _classified_landing(landing, [_DECLARED_FILES])
    assert ln.resolved_tags == ["monitor-bridge"]
    classify.shortcut_if_nothing(ln)
