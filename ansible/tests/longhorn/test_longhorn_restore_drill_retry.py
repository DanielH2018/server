#!/usr/bin/env python3
"""The restore drill's retry: a failed attempt is re-drilled once, ahead of the rotation.

Selection is least-recently-attempted, and the attempt stamp is refreshed whatever the outcome,
so a failed volume's next slot was a whole rotation away — 26 nights against check 8's 31-day
coverage window. tdarr-configs (2026-09-07, byte floor) and n8n-files (2026-08-30, empty volume)
both paged that way, and neither could be re-proven without a root shell (#2270).

The bound is the half that matters: a volume that fails every time it is drilled must not take
every night, which is the property the attempt-stamp selection was built for in the first place.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_retry.py
"""

import os
from pathlib import Path

import pytest

from _restore_drill import harness
from _restore_drill import selected

OLDEST = "a-config"  # the rotation's own pick
FAILED = "b-config"  # attempted more recently than OLDEST, and that attempt failed

_OLD_AT = 1_700_000_000
_FAILED_AT = 1_750_000_000


def _stamp(path: Path, when: int, content: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else "")
    os.utime(path, (when, when))


@pytest.fixture
def drill(tmp_path: Path):
    """OLDEST succeeded long ago; FAILED was attempted since and never succeeded."""
    run = harness(tmp_path, [OLDEST, FAILED])
    _stamp(run.stamp_dir / "attempts" / OLDEST, _OLD_AT)
    _stamp(run.stamp_dir / "success" / OLDEST, _OLD_AT, f"{_OLD_AT}\n")
    _stamp(run.stamp_dir / "attempts" / FAILED, _FAILED_AT)
    return run


def test_a_failed_attempt_is_redrilled_ahead_of_the_rotation(drill) -> None:
    """The issue's verify-by: the failure jumps the queue rather than waiting out a cycle."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    assert selected(drill, drill()) == FAILED


def test_a_retry_already_spent_leaves_the_rotation_alone(drill) -> None:
    """The bound: one retry per failed attempt, so a broken volume cannot take every night."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    _stamp(drill.stamp_dir / "retries" / FAILED, _FAILED_AT + 1)
    assert selected(drill, drill()) == OLDEST


def test_the_retry_marker_is_written_when_the_retry_runs(drill) -> None:
    """What spends the budget. Written on the attempt, so a failed retry is still spent."""
    drill()
    marker = drill.stamp_dir / "retries" / FAILED
    assert marker.exists()
    assert (
        marker.stat().st_mtime
        >= (drill.stamp_dir / "attempts" / FAILED).stat().st_mtime
    )


def test_a_volume_that_succeeded_is_not_owed_a_retry(drill) -> None:
    """A success at or after the attempt is that attempt's own success."""
    _stamp(drill.stamp_dir / "success" / FAILED, _FAILED_AT, f"{_FAILED_AT}\n")
    assert selected(drill, drill()) == OLDEST


def test_a_stale_success_does_not_cover_a_later_failed_attempt(drill) -> None:
    """The comparison is success-content vs attempt-mtime, not merely "a success exists"."""
    _stamp(drill.stamp_dir / "success" / FAILED, _OLD_AT, f"{_OLD_AT}\n")
    assert selected(drill, drill()) == FAILED


def test_a_pinned_run_does_not_spend_the_retry(drill) -> None:
    """A pin filters the eligible set to one name, so the scan would always name the pin."""
    assert selected(drill, drill([OLDEST])) == OLDEST
    assert not (drill.stamp_dir / "retries").exists() or not list(
        (drill.stamp_dir / "retries").iterdir()
    )
