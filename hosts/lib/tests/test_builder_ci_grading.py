"""The builder's CI grading and its metric reasons, held to `hosts/lib/ci_verdict.py` and the plan's.

`builder_ci_grade.py` carries its own copy of `ci_verdict` and the two conclusion sets. These
tests hold the copy equal to `hosts/lib/ci_verdict.py`.

Run: uv run pytest hosts/lib/tests/test_builder_ci_grading.py
"""

import builder_ci_grade as cg
import host_package_builder as hb
import pytest
import ci_verdict as shared_gate

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
def test_the_grading_matches_the_shared_gate(runs):
    assert cg.ci_verdict(runs, frozenset({"prek"})) == shared_gate.ci_verdict(
        runs, frozenset({"prek"})
    )


def test_the_conclusion_sets_match_the_shared_gate():
    assert cg._CI_PASS_CONCLUSIONS == shared_gate._CI_PASS_CONCLUSIONS
    assert cg._CI_NO_VERDICT_CONCLUSIONS == shared_gate._CI_NO_VERDICT_CONCLUSIONS
