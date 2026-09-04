"""The staging vocabularies are `StrEnum`s whose values are byte-identical to the old literals.

`StagingVerdict` and `TickOutcome` replaced eight bare module constants. The type is the gain:
`ty` now catches a typo that a runtime `frozenset` only caught when the branch ran. The VALUES
are what must not move — they are the journal line an operator reads, the `verdict`/`outcome`
fields `record_staging_tick` writes into `staging-ticks.jsonl`, and what
`backfill_staging_gate.py` reads back out of that file in the other tree.

So this module pins the values against the literals they replaced, written out here rather than
read from the enum: a test that compares the enum to itself would accept a renamed value.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_staging_vocabulary_is_a_strenum.py
"""

import json
from enum import StrEnum

import pytest

from deploy_staging import (
    STAGING_NO_VERDICT,
    STAGING_PASS,
    STAGING_REJECTED,
    STAGING_SKIPPED,
    TICK_FALSE_FAILURE,
    TICK_NEEDS_TRIAGE,
    TICK_OK,
    StagingVerdict,
    TickOutcome,
    staging_tick_outcome,
    staging_verdict,
)


# The literals as they read before the enum landed, by the name each was assigned to.
OLD_STAGING_LITERALS = {
    "STAGING_PASS": "pass",
    "STAGING_REJECTED": "rejected",
    "STAGING_NO_VERDICT": "no_verdict",
    "STAGING_SKIPPED": "skipped",
}
OLD_TICK_LITERALS = {
    "TICK_OK": "pass",
    "TICK_FALSE_FAILURE": "false-failure",
    "TICK_NEEDS_TRIAGE": "needs-triage",
}
LIVE = {
    "STAGING_PASS": STAGING_PASS,
    "STAGING_REJECTED": STAGING_REJECTED,
    "STAGING_NO_VERDICT": STAGING_NO_VERDICT,
    "STAGING_SKIPPED": STAGING_SKIPPED,
    "TICK_OK": TICK_OK,
    "TICK_FALSE_FAILURE": TICK_FALSE_FAILURE,
    "TICK_NEEDS_TRIAGE": TICK_NEEDS_TRIAGE,
}


# ── the values did not move ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("name", "literal"), sorted((OLD_STAGING_LITERALS | OLD_TICK_LITERALS).items())
)
def test_each_constant_still_equals_the_literal_it_replaced(name, literal):
    assert LIVE[name] == literal


def test_the_enums_carry_no_member_the_old_vocabulary_lacked():
    """A new member is a word the backfill tree does not know. Named sets, not counts."""
    assert {v.value for v in StagingVerdict} == set(OLD_STAGING_LITERALS.values())
    assert {v.value for v in TickOutcome} == set(OLD_TICK_LITERALS.values())


# ── the type is what changed ──────────────────────────────────────────────────────────────
def test_both_vocabularies_are_strenums():
    assert issubclass(StagingVerdict, StrEnum) and issubclass(TickOutcome, StrEnum)


def test_the_verdict_functions_return_members_not_bare_strings():
    """Returning a member is what gives `ty` something to check at the call sites."""
    assert isinstance(staging_verdict(0, 0), StagingVerdict)
    assert isinstance(staging_tick_outcome(STAGING_REJECTED), TickOutcome)


# ── the on-disk form is identical ─────────────────────────────────────────────────────────
def test_a_member_serialises_to_the_bare_word():
    """`record_staging_tick` json.dumps the verdict and the outcome straight into the ledger.

    A member that serialised as `"StagingVerdict.PASS"` would break every consumer of
    `staging-ticks.jsonl` while every equality test above still passed.
    """
    row = json.dumps(
        {
            "verdict": staging_verdict(1, 2),
            "outcome": staging_tick_outcome(STAGING_PASS),
        }
    )
    assert row == '{"verdict": "rejected", "outcome": "pass"}'


def test_a_plain_string_key_still_reaches_the_outcome_table():
    """The tick ledger is read back as plain JSON strings, never as members."""
    assert staging_tick_outcome("no_verdict") == TICK_FALSE_FAILURE
    assert staging_tick_outcome("skipped") is None


# ── proof the value check can go red ──────────────────────────────────────────────────────
def test_a_renamed_value_is_flagged():
    """The rejecting half: a member whose value drifted must fail the comparison above."""

    class Drifted(StrEnum):
        PASS = "passed"  # one character, and every ledger row stops classifying
        REJECTED = "rejected"
        NO_VERDICT = "no_verdict"
        SKIPPED = "skipped"

    assert {v.value for v in Drifted} != set(OLD_STAGING_LITERALS.values())
    assert Drifted.PASS != OLD_STAGING_LITERALS["STAGING_PASS"]
