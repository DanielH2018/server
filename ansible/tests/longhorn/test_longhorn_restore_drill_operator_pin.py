#!/usr/bin/env python3
"""The restore drill's operator pin path: one volume on demand, without steering the rotation.

Until 2026-09-21 `PIN` was rendered from `k3s_longhorn_restore_drill_pvc` and nothing else read
it, so `PIN=<pvc> longhorn-restore-drill.sh` was overwritten on line 26 and the rotation's own
pick was drilled instead. PR #2182 printed exactly that non-working command as a remediation.
The only way to drill one volume was to backdate its attempt stamp so the next nightly run
picked it (#2183).

These tests RUN the rendered script rather than grep it, through the shared harness in
`_restore_drill.py`.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill_operator_pin.py
"""

import os
from pathlib import Path

import pytest

from _restore_drill import harness
from _restore_drill import selected

OLDEST = "a-config"  # the rotation's own pick: oldest attempt stamp
PINNED = "b-config"  # attempted more recently, so never the rotation's pick
UNBACKED = "c-config"  # in a backup group but with no Completed backup


@pytest.fixture
def drill(tmp_path: Path):
    run = harness(tmp_path, [OLDEST, PINNED, UNBACKED], backed=[OLDEST, PINNED])
    attempts = run.stamp_dir / "attempts"
    success = run.stamp_dir / "success"
    success.mkdir(parents=True)
    # Both attempts SUCCEEDED, so neither is owed the retry that would otherwise outrank the
    # rotation's pick and make every assertion below about the wrong volume.
    for pvc, when in ((OLDEST, 1_700_000_000), (PINNED, 1_750_000_000)):
        (attempts / pvc).touch()
        os.utime(attempts / pvc, (when, when))
        (success / pvc).write_text(f"{when}\n")
    return run


def _attempted(run, pvc: str) -> float:
    return (run.stamp_dir / "attempts" / pvc).stat().st_mtime


def test_nightly_run_drills_the_rotations_own_pick(drill) -> None:
    """Control: with no pin the least-recently-attempted volume is selected."""
    assert selected(drill, drill()) == OLDEST


def test_argv_pin_drills_that_volume_not_the_rotations_pick(drill) -> None:
    """`longhorn-restore-drill.sh <pvc>` reaches the selection; the oldest stamp is untouched."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    before = _attempted(drill, OLDEST)
    assert selected(drill, drill([PINNED])) == PINNED
    assert _attempted(drill, OLDEST) == before


def test_env_pin_drills_that_volume(drill) -> None:
    """`RESTORE_DRILL_PIN=<pvc>` is the same override for a caller that cannot pass argv."""
    assert selected(drill, drill(env={"RESTORE_DRILL_PIN": PINNED})) == PINNED


def test_argv_beats_env(drill) -> None:
    assert selected(drill, drill([PINNED], env={"RESTORE_DRILL_PIN": OLDEST})) == PINNED


def test_pin_leaves_the_published_candidate_list_whole(drill) -> None:
    """Check 8 sizes its coverage window from this file; a one-line list would shrink it."""
    drill([PINNED])
    assert (drill.stamp_dir / "candidates").read_text().split() == [OLDEST, PINNED]


def test_pin_naming_an_ineligible_volume_fails_naming_it(drill) -> None:
    """A pin with no Completed backup fails closed, naming the pin, and stamps no attempt."""
    proc = drill([UNBACKED])
    assert proc.returncode == 1
    assert f"pinned volume {UNBACKED} is not eligible" in proc.stderr, proc.stderr
    assert not (drill.stamp_dir / "attempts" / UNBACKED).exists()


def test_operator_pin_writes_the_volume_stamp_but_not_the_liveness_stamp(drill) -> None:
    """The issue's verify-by: `success/<pvc>` is written.

    `last-success` is check 7's "is the nightly drill alive", and a hand run must not refresh
    it over a dead cron.
    """
    proc = drill([PINNED], restore="pass")
    assert proc.returncode == 0, proc.stderr
    assert (drill.stamp_dir / "success" / PINNED).exists()
    assert not (drill.stamp_dir / "last-success").exists()


def test_nightly_run_writes_both_stamps(drill) -> None:
    proc = drill(restore="pass")
    assert proc.returncode == 0, proc.stderr
    assert (drill.stamp_dir / "success" / OLDEST).exists()
    assert (drill.stamp_dir / "last-success").exists()
