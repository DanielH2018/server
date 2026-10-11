"""`lib/exit_codes.py`: the frozensets and the individual names must not drift apart.

The failure guarded is the one the module exists to close. `DEPLOY_SH_NO_VERDICT` and the
individual `DEPLOY_*` names describe the same contract twice, so a change to one that misses
the other is exactly the drift this module exists to prevent.

Every rule has a reject half, per CLAUDE.md: a set that quietly stopped containing a member
and a set that quietly gained one are both invisible from a `<=` assertion alone.

Run: uv run pytest scripts/lib/tests/test_exit_codes.py
"""

from pathlib import Path

import pytest


from lib import exit_codes as ec


def test_the_no_verdict_set_is_exactly_the_refusals_by_name():
    """`==`, not `<=`: a member dropped or added must both fail."""
    assert ec.DEPLOY_SH_NO_VERDICT == {
        ec.DEPLOY_TAG_MISS,
        ec.DEPLOY_BROAD,
        ec.DEPLOY_STALE,
        ec.DEPLOY_LOCK_BUSY,
        ec.DEPLOY_LOCK_UNAVAILABLE,
        ec.DEPLOY_SNAPSHOT_FAILED,
        ec.DEPLOY_NO_HOSTS,
        ec.DEPLOY_LOCK_PLAN_FAILED,
    }


@pytest.mark.parametrize(
    "name",
    [
        "DEPLOY_TAG_MISS",
        "DEPLOY_BROAD",
        "DEPLOY_STALE",
        "DEPLOY_LOCK_BUSY",
        "DEPLOY_LOCK_UNAVAILABLE",
        "DEPLOY_SNAPSHOT_FAILED",
        "DEPLOY_NO_HOSTS",
        "DEPLOY_LOCK_PLAN_FAILED",
    ],
)
def test_each_named_refusal_is_in_the_no_verdict_set(name):
    assert getattr(ec, name) in ec.DEPLOY_SH_NO_VERDICT


@pytest.mark.parametrize("name", ["DEPLOY_OK", "DEPLOY_PLAYBOOK_FAILED"])
def test_the_codes_that_are_not_refusals_stay_out_of_the_set(name):
    """The reject half. 20 in particular means changes ARE live -- never a resume point."""
    assert getattr(ec, name) not in ec.DEPLOY_SH_NO_VERDICT


def test_the_playbook_failure_code_is_disjoint_from_every_wrapper_refusal():
    """The disjointness is asserted here too."""
    assert ec.DEPLOY_PLAYBOOK_FAILED not in ec.DEPLOY_SH_NO_VERDICT
    assert ec.DEPLOY_PLAYBOOK_FAILED != ec.DEPLOY_OK


def test_the_front_half_refuses_with_the_named_codes():
    """Both Python halves import their codes rather than restating them."""
    from deploy_tools.deploy_lib import run as deploy_run

    assert deploy_run.DEPLOY_TAG_MISS is ec.DEPLOY_TAG_MISS
    assert deploy_run.DEPLOY_STALE is ec.DEPLOY_STALE
    assert deploy_run.DEPLOY_BAD_FLAGS is ec.DEPLOY_BAD_FLAGS
    from deploy_tools.deploy_lib import under_locks as locked

    assert locked.DEPLOY_PLAYBOOK_FAILED is ec.DEPLOY_PLAYBOOK_FAILED
    assert locked.DEPLOY_NO_HOSTS is ec.DEPLOY_NO_HOSTS
    assert locked.DEPLOY_LOCK_PLAN_FAILED is ec.DEPLOY_LOCK_PLAN_FAILED


def test_the_broad_refusal_is_returned_by_name_from_deploy_tags():
    """3 reaches deploy.sh through `exit "$status"` from `deploy_tags.py changed`, so the
    wrapper has no `exit 3` literal to grep. The producer returns the constant instead:
    every broad refusal is `return DEPLOY_BROAD`, and no literal `return 3` remains."""
    text = (
        Path(__file__).resolve().parents[2] / "deploy_tools" / "deploy_lib" / "tags.py"
    ).read_text()
    assert "from lib.exit_codes import DEPLOY_BROAD" in text
    assert "return DEPLOY_BROAD" in text
    assert "return 3\n" not in text


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
        ("CI_GREEN", "CI_RED", "CI_DISARMED", "CI_PENDING"),
        ("LAND_SETTLED", "LAND_FAILED", "LAND_BAD_ARGS", "LAND_GAVE_UP"),
        (
            "DEPLOY_OK",
            "DEPLOY_TAG_MISS",
            "DEPLOY_BROAD",
            "DEPLOY_STALE",
            "DEPLOY_PLAYBOOK_FAILED",
            "DEPLOY_BAD_FLAGS",
            "DEPLOY_LOCK_BUSY",
            "DEPLOY_LOCK_UNAVAILABLE",
        ),
    ],
)
def test_no_contract_reuses_a_value_within_itself(group):
    """Two names for one integer inside ONE vocabulary is a bug; across vocabularies it is not."""
    values = [getattr(ec, name) for name in group]
    assert len(set(values)) == len(values), dict(zip(group, values, strict=True))


def test_the_importers_take_their_values_from_here():
    """Non-vacuity: the module is pointless if a consumer still carries its own copy."""
    from deploy_tools.deploy_lib import tags as deploy_tags
    from deploy_tools.narrow_lib import broad as narrow_broad

    assert deploy_tags.DEPLOY_BROAD is ec.DEPLOY_BROAD
    assert narrow_broad.DEPLOY_OK is ec.DEPLOY_OK


# -- the sysexits spine --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spine"),
    [
        ("DEPLOY_BAD_FLAGS", "USAGE_ERROR"),
        ("LAND_BAD_ARGS", "USAGE_ERROR"),
        ("DEPLOY_LOCK_BUSY", "TEMP_FAIL"),
        ("LAND_GAVE_UP", "TEMP_FAIL"),
        ("TICK_STILL_RUNNING", "TEMP_FAIL"),
        ("CI_PENDING", "TEMP_FAIL"),
    ],
)
def test_each_family_takes_its_usage_and_temp_fail_from_the_spine(name, spine):
    """One coding across the repo: 64 is always a usage error, 75 always a temporary failure.

    `LAND_BAD_ARGS` is the member this was filed for -- it was 2, argparse's own
    code, while `deploy.sh` had always used 64 for the same answer.
    """
    assert getattr(ec, name) == getattr(ec, spine)


def test_the_spine_holds_the_sysexits_values():
    """A rename inside the module must not silently renumber every family at once."""
    assert (ec.OK, ec.FAILED, ec.USAGE_ERROR, ec.TEMP_FAIL) == (0, 1, 64, 75)


# -- CONTRACTS, the one renderable copy ----------------------------------------------------


def _declared_values(prefix):
    return {
        value
        for name, value in vars(ec).items()
        if name.startswith(prefix) and isinstance(value, int)
    }


@pytest.mark.parametrize(
    ("entry_point", "prefix"),
    [
        ("scripts/deploy.sh", "DEPLOY_"),
        ("scripts/deploy_tools/land.sh", "LAND_"),
        ("scripts/deploy_tools/gitops_tick.sh", "TICK_"),
        ("scripts/deploy_tools/await_ci.py", "CI_"),
    ],
)
def test_every_declared_code_has_a_contract_row(entry_point, prefix):
    """A code added above without a row is a code `describe` cannot explain.

    That is the whole failure mode this table replaces: 77 and 78 each reached production as a
    bare integer while every prose copy of the table still listed seven.
    """
    rows = {code.value for code in ec.contract(entry_point)}
    assert _declared_values(prefix) == rows


def test_publish_prs_two_vocabularies_share_one_contract_row_set():
    """`publish` and `unlanded` reuse 0-3 deliberately, so their rows are merged, not doubled."""
    rows = {code.value for code in ec.contract("scripts/deploy_tools/publish_pr.py")}
    assert rows == _declared_values("PUBLISH_") | _declared_values("UNLANDED_")


def test_every_contract_row_names_a_constant_that_exists():
    for entry_point, codes in ec.CONTRACTS.items():
        for code in codes:
            assert getattr(ec, code.const) == code.value, f"{entry_point}: {code.const}"


def test_describe_renders_the_constant_the_value_and_the_remedy():
    note = ec.describe("scripts/deploy.sh", ec.DEPLOY_STALE)
    assert note is not None
    assert note.startswith("DEPLOY_STALE (4): ")
    assert "NOTHING was deployed" in note and "Pull first" in note


def test_describe_returns_none_for_a_code_outside_the_contract():
    """The reject half: a wrapper must print the bare code rather than a wrong meaning."""
    assert ec.describe("scripts/deploy.sh", 200) is None
    assert ec.describe("scripts/nothing.sh", 1) is None


def test_every_no_verdict_code_carries_a_remedy():
    """A resume point with no next step is the `Exit code N` an operator had to look up."""
    rows = {c.value: c for c in ec.contract("scripts/deploy.sh")}
    assert all(rows[value].remedy for value in ec.DEPLOY_SH_NO_VERDICT)
