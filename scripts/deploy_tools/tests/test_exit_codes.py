"""`exit_codes.py`: the frozensets and the individual names must not drift apart.

The failure guarded is the one the module exists to close. `DEPLOY_SH_NO_VERDICT` and the
individual `DEPLOY_*` names describe the same contract twice, so a change to one that misses
the other is exactly the drift `staging_gate.py` and `land_lib/deploy.py` had between them
before this module existed.

Every rule has a reject half, per CLAUDE.md: a set that quietly stopped containing a member
and a set that quietly gained one are both invisible from a `<=` assertion alone.

Run: uv run pytest scripts/deploy_tools/tests/test_exit_codes.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/

from deploy_tools import exit_codes as ec


def test_the_no_verdict_set_is_exactly_the_four_refusals_by_name():
    """`==`, not `<=`: a member dropped or added must both fail."""
    assert ec.DEPLOY_SH_NO_VERDICT == {
        ec.DEPLOY_TAG_MISS,
        ec.DEPLOY_BROAD,
        ec.DEPLOY_STALE,
        ec.DEPLOY_LOCK_BUSY,
    }


@pytest.mark.parametrize(
    "name",
    ["DEPLOY_TAG_MISS", "DEPLOY_BROAD", "DEPLOY_STALE", "DEPLOY_LOCK_BUSY"],
)
def test_each_named_refusal_is_in_the_no_verdict_set(name):
    assert getattr(ec, name) in ec.DEPLOY_SH_NO_VERDICT


@pytest.mark.parametrize("name", ["DEPLOY_OK", "DEPLOY_PLAYBOOK_FAILED"])
def test_the_codes_that_are_not_refusals_stay_out_of_the_set(name):
    """The reject half. 20 in particular means changes ARE live -- never a resume point."""
    assert getattr(ec, name) not in ec.DEPLOY_SH_NO_VERDICT


def test_the_playbook_failure_code_is_disjoint_from_every_wrapper_refusal():
    """That disjointness IS the 2026-09-02 fix for issue #840, so it is asserted here too."""
    assert ec.DEPLOY_PLAYBOOK_FAILED not in ec.DEPLOY_SH_NO_VERDICT
    assert ec.DEPLOY_PLAYBOOK_FAILED != ec.DEPLOY_OK


def test_the_deploy_sh_values_match_the_wrapper_itself():
    """Read off `scripts/deploy.sh`: the two constants it defines, and its `exit` literals."""
    text = (Path(__file__).resolve().parents[3] / "scripts" / "deploy.sh").read_text()
    assert f"LOCK_BUSY={ec.DEPLOY_LOCK_BUSY}" in text
    assert f"PLAYBOOK_FAILED={ec.DEPLOY_PLAYBOOK_FAILED}" in text
    for rc in (ec.DEPLOY_TAG_MISS, ec.DEPLOY_STALE, ec.DEPLOY_BAD_FLAGS):
        assert f"exit {rc}\n" in text, f"deploy.sh no longer exits {rc}"


@pytest.mark.parametrize(
    "group",
    [
        ("PUBLISH_PUBLISHED", "PUBLISH_STILL_LOCAL", "PUBLISH_PUSHED_NO_PR"),
        (
            "UNLANDED_NOTHING",
            "UNLANDED_ORIGIN_UNREADABLE",
            "UNLANDED_PR_OPEN",
            "UNLANDED_NO_PR",
        ),
        ("GATE_PASS", "GATE_REJECTED", "GATE_NO_VERDICT", "GATE_NOT_RUN"),
        ("CI_GREEN", "CI_RED", "CI_DISARMED", "CI_PENDING"),
        ("LAND_SETTLED", "LAND_FAILED", "LAND_BAD_ARGS", "LAND_GAVE_UP"),
    ],
)
def test_no_contract_reuses_a_value_within_itself(group):
    """Two names for one integer inside ONE vocabulary is a bug; across vocabularies it is not."""
    values = [getattr(ec, name) for name in group]
    assert len(set(values)) == len(values), dict(zip(group, values, strict=True))


def test_the_importers_take_their_values_from_here():
    """Non-vacuity: the module is pointless if a consumer still carries its own copy."""
    from deploy_tools import staging_gate

    assert staging_gate.DEPLOY_SH_NO_VERDICT is ec.DEPLOY_SH_NO_VERDICT
    assert staging_gate.PASS == ec.GATE_PASS
    assert staging_gate.NOT_RUN == ec.GATE_NOT_RUN
