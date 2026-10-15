"""The builder's CI grading and its metric reasons, held to the deployer's and the plan's.

The builder cannot import `deploy_git`, so it carries its own copy of `ci_verdict` and the
two conclusion sets. These tests hold the copy equal to the deployer's.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_builder_ci_grading.py
"""

import host_package_builder as hb
import pytest
from deployer_lib import deploy_git

GREEN = [{"name": "prek", "status": "completed", "conclusion": "success"}]
RED = [{"name": "prek", "status": "completed", "conclusion": "failure"}]
PENDING = [{"name": "prek", "status": "in_progress", "conclusion": None}]


def test_every_reason_is_a_plan_value_and_none_is_trust():
    assert "trust" not in hb.REASONS
    assert hb.REASONS <= {
        "hold",
        "ancestry",
        "keyring",
        "ci",
        "no-green",
        "token",
        "tools",
        "canary",
        "ok",
        "build",
    }


@pytest.mark.parametrize(
    "runs",
    [
        GREEN,
        RED,
        PENDING,
        [],
        GREEN + RED,
        [{"name": "prek", "status": "completed", "conclusion": "cancelled"}],
        [{"name": "other", "status": "completed", "conclusion": "failure"}],
    ],
)
def test_the_grading_matches_the_deployers(runs):
    assert hb.ci_verdict(runs, frozenset({"prek"})) == deploy_git.ci_verdict(
        runs, frozenset({"prek"})
    )


def test_the_conclusion_sets_match_the_deployers():
    assert hb._CI_PASS_CONCLUSIONS == deploy_git._CI_PASS_CONCLUSIONS
    assert hb._CI_NO_VERDICT_CONCLUSIONS == deploy_git._CI_NO_VERDICT_CONCLUSIONS
