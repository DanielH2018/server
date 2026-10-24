#!/usr/bin/env python3
"""The restore drill's retry: a failed attempt is re-drilled once, ahead of the rotation.

Selection is least-recently-attempted, and the attempt stamp is refreshed whatever the outcome,
so without a retry a failed volume's next slot is a whole rotation away — 26 nights against
check 8's 31-day coverage window.

The bound is the half that matters: a volume that fails every time it is drilled must not take
every night, which is the property the attempt-stamp selection was built for in the first place.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_retry.py
"""

from pathlib import Path

import pytest

from _restore_drill import harness
from _restore_drill import selected

OLDEST = "a-config"  # the rotation's own pick
FAILED = "b-config"  # attempted more recently than OLDEST, and that attempt failed

_OLD_AT = 1_700_000_000
_FAILED_AT = 1_750_000_000


@pytest.fixture
def drill(tmp_path: Path):
    """OLDEST succeeded long ago; FAILED was attempted since and never succeeded."""
    run = harness(tmp_path, [OLDEST, FAILED])
    run.seed(
        {
            f"attempts.{OLDEST}": _OLD_AT,
            f"success.{OLDEST}": _OLD_AT,
            f"attempts.{FAILED}": _FAILED_AT,
        }
    )
    return run


def test_a_failed_attempt_is_redrilled_ahead_of_the_rotation(drill) -> None:
    """The issue's verify-by: the failure jumps the queue rather than waiting out a cycle."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    assert selected(drill, drill()) == FAILED


def test_a_retry_already_spent_leaves_the_rotation_alone(drill) -> None:
    """The bound: one retry per failed attempt, so a broken volume cannot take every night."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    drill.seed({f"retries.{FAILED}": _FAILED_AT + 1})
    assert selected(drill, drill()) == OLDEST


def test_the_retry_marker_is_written_when_the_retry_runs(drill) -> None:
    """What spends the budget. Written on the attempt, so a failed retry is still spent."""
    drill()
    assert drill.epoch(f"retries.{FAILED}") >= drill.epoch(f"attempts.{FAILED}")
    assert drill.epoch(f"attempts.{FAILED}") > _FAILED_AT


def test_a_volume_that_succeeded_is_not_owed_a_retry(drill) -> None:
    """A success at or after the attempt is that attempt's own success."""
    drill.seed({f"success.{FAILED}": _FAILED_AT})
    assert selected(drill, drill()) == OLDEST


def test_a_stale_success_does_not_cover_a_later_failed_attempt(drill) -> None:
    """The comparison is success epoch vs attempt epoch, not merely "a success exists"."""
    drill.seed({f"success.{FAILED}": _OLD_AT})
    assert selected(drill, drill()) == FAILED


def test_a_pinned_run_does_not_spend_the_retry(drill) -> None:
    """A pin filters the eligible set to one name, so the scan would always name the pin."""
    assert selected(drill, drill([OLDEST])) == OLDEST
    assert not [key for key in drill.cm if key.startswith("retries.")]
