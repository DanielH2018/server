#!/usr/bin/env python3
"""Guards on check 4's first-run grace: a volume with NO backup must stay visible while excused.

The grace itself is correct and deliberate — a service deployed this afternoon must not redden the
backup plane before its first scheduled run. What must not happen is a SILENT excuse: a branch ending in a
bare `continue`, with `CHECKED` incremented below it, makes the green message bit-for-bit
identical whether or not the volume existed. The covered count does not
fall when a volume is graced; it fails to rise, and nobody can see a number that did not change.

The weekly shard cadence puts a volume's first run up to six days out, so a graced volume can
have no offsite copy for days, not hours.

The grace mirrors what the DISARMED path does: count it, and name it in the green message.
Three properties are load-bearing, and a future edit could plausibly break any of them:

COUNTED. The grace branch must increment `graced` before it excuses the volume, or the state is
invisible again.

SURFACED. `graced_new`/`graced_seeded` must reach the pushed green message via build_verdict().
A counter nothing prints is the same silence with extra steps.

NAMED. The volumes are listed, not just totalled. A graced volume has no offsite copy at all, so
"3 volume(s) awaiting" tells an operator nothing they can act on — and an unactionable red is what
check 3's own comment records as the thing that stops being read.

check 4 lives in longhorn_backup_health_logic.check_tier()/build_verdict(); these guards run
against those functions directly.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_backup_grace_visibility.py
"""

import sys

from lib.repo_paths import K3S_FILES

sys.path.insert(0, str(K3S_FILES))
from longhorn_lib import longhorn_backup_health_logic as logic

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _graced_result():
    """A single volume created 10 minutes ago, no backup anywhere, first run 3h from now."""
    result = logic.TierResult()
    created = "2027-01-15T07:50:00Z"
    rows = [("pvc-1", created, "ns/claim1", "default")]
    logic.check_tier(
        result, rows, [], 30 * 3600, "daily", "daily-backup", "11:00", "*", set(), NOW
    )
    return result


def test_the_grace_branch_counts_rather_than_dropping_silently():
    """A graced volume increments `graced`; a bare drop hides the volume."""
    result = _graced_result()
    assert result.graced == 1, (
        "check 4's first-run grace must count the volume it excuses — dropping it silently "
        "leaves a volume with no backup anywhere completely invisible"
    )


def test_graced_volumes_are_named_not_just_counted():
    result = _graced_result()
    assert result.graced_vols == ["ns/claim1"], (
        "name the graced volumes — a bare count is not actionable, and a volume in this "
        "state has no offsite copy at all"
    )


def test_graced_reaches_the_green_message():
    """A counter that never prints is the same silence with extra steps."""
    status, msg, _push_msg = logic.build_verdict(
        [],
        backup_targets=["default"],
        disarmed_targets=[],
        age_s=0,
        checked=0,
        recent_n=0,
        daily_backup_budget=16,
        suppressed=0,
        graced_new=1,
        graced_new_vols=["ns/claim1"],
        graced_seeded=0,
        graced_seeded_vols=[],
    )
    assert status == "up"
    assert "ns/claim1" in msg, (
        "the graced volume list must reach the green message the way SUPPRESSED does via "
        "the DISARMED note — otherwise the tile reads identical while volumes sit unprotected"
    )


def test_checked_is_not_incremented_on_the_grace_path():
    """The regression that made this invisible: `checked` counts only volumes with a backup.

    This is the property that makes the green count misleading on its own, and it is CORRECT —
    a graced volume genuinely is not covered. It is asserted here so that a future 'fix' that
    papers over the silence by counting graced volumes as covered fails loudly instead.
    """
    result = _graced_result()
    assert result.checked == 0, (
        "a volume with no backup must never count toward the covered total — that would "
        "turn an invisible gap into an actively false one"
    )


def test_both_silent_skips_are_surfaced():
    """DISARMED and GRACED are the only two paths that excuse a volume; both must be named."""
    _status, msg, _push_msg = logic.build_verdict(
        [],
        backup_targets=["default"],
        disarmed_targets=["r2"],
        age_s=0,
        checked=0,
        recent_n=0,
        daily_backup_budget=16,
        suppressed=2,
        graced_new=1,
        graced_new_vols=["ns/claim1"],
        graced_seeded=0,
        graced_seeded_vols=[],
    )
    assert "DISARMED" in msg and "ns/claim1" in msg, (
        "every path that excuses a volume has to say so in the green message, or the tile "
        "reports coverage it does not have"
    )
