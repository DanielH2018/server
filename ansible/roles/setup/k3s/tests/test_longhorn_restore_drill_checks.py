#!/usr/bin/env python3
"""Executing tests for the heartbeat's restore-drill arms: checks 7 and 8, and the oversize and no-PVC checks.

Split out of `test_longhorn_backup_health.py` when it reached its line cap, with the same
`..._is_clean` / `..._is_flagged` pairing. The stamp files these arms read are pinned by real
subprocess runs in `test_longhorn_backup_health_reader.py`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_restore_drill_checks.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
import longhorn_backup_health_logic as logic

HOUR = 3600.0
DAY = 86400.0
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z-ish; fixed so every test is deterministic.


# ── check 7: restore-drill freshness ─────────────────────────────────────────────────────────


def test_restore_drill_is_clean_when_recent():
    recent = str(int(NOW - HOUR))
    assert logic.check_restore_drill(recent, "/stamp", NOW, 3 * DAY, 3) is None


def test_restore_drill_is_flagged_when_the_stamp_is_missing():
    """FAILS CLOSED: a never-run drill is the state most in need of reporting."""
    problem = logic.check_restore_drill(
        None, "/var/lib/longhorn-restore-drill/last-success", NOW, 3 * DAY, 3
    )
    assert problem == (
        3,
        "no restore drill has ever succeeded (no /var/lib/longhorn-restore-drill/last-success) — "
        "backups are unproven",
    )


def test_restore_drill_is_flagged_on_an_unparseable_stamp():
    assert logic.check_restore_drill("not-a-number", "/stamp", NOW, 3 * DAY, 3) == (
        3,
        "restore-drill stamp content is not a valid timestamp — "
        "treating the restore path as unproven",
    )


def test_restore_drill_is_flagged_when_the_stamp_is_unreadable():
    """Distinct from a MISSING stamp — see check_restore_drill's docstring for the 2026-08-19
    incident this distinction exists to prevent from repeating: a stamp that exists but can't be
    opened must not read as "the drill never ran"."""
    problem = logic.check_restore_drill(
        None,
        "/var/lib/longhorn-restore-drill/last-success",
        NOW,
        3 * DAY,
        3,
        stamp_unreadable=True,
    )
    assert problem == (
        3,
        "restore-drill stamp at /var/lib/longhorn-restore-drill/last-success could not be "
        "read (permissions?) — treating the restore path as unproven",
    )


def test_restore_drill_is_flagged_when_stale():
    stale = str(int(NOW - 5 * DAY))
    problem = logic.check_restore_drill(stale, "/stamp", NOW, 3 * DAY, 3)
    assert problem == (3, "last successful restore drill was 5d ago (limit 3d)")


def test_restore_drill_pages_below_backup_failure_severity():
    """A stale drill is an assurance gap, not an active failure — rank 3, never rank 2."""
    for content in (None, "garbage", str(int(NOW - 5 * DAY))):
        problem = logic.check_restore_drill(content, "/stamp", NOW, 3 * DAY, 3)
        assert problem is not None and problem[0] == 3


# ── check 8: restore-drill rotation coverage ─────────────────────────────────────────────────


def test_restore_coverage_is_clean_with_no_candidates():
    assert logic.check_restore_coverage([], {}, {}, NOW, 5) is None


def test_restore_coverage_is_clean_when_within_the_derived_window():
    # 3 candidates + 5 days slack = 8-day window.
    seen = {"pvc-1": NOW - 2 * DAY}
    assert (
        logic.check_restore_coverage(["pvc-1", "pvc-2", "pvc-3"], seen, {}, NOW, 5)
        is None
    )


def test_restore_coverage_is_flagged_past_the_window_with_no_success():
    coverage_days = 3 + 5  # cand_n=3, slack=5
    seen = {"pvc-1": NOW - (coverage_days + 1) * DAY}
    problem = logic.check_restore_coverage(
        ["pvc-1", "pvc-2", "pvc-3"], seen, {}, NOW, 5
    )
    assert problem == (3, "volume(s) not restore-proven in 8d: pvc-1")


def test_restore_coverage_grace_is_per_candidate_from_its_own_join_marker():
    """A rotation-wide start date would page every newly added volume for a full cycle."""
    coverage_days = 3 + 5
    seen = {
        "pvc-old": NOW - (coverage_days + 1) * DAY,  # joined long ago, past its window
        "pvc-new": NOW - DAY,  # joined yesterday, well within its own window
    }
    problem = logic.check_restore_coverage(["pvc-old", "pvc-new"], seen, {}, NOW, 5)
    assert problem is not None
    assert "pvc-old" in problem[1]
    assert "pvc-new" not in problem[1]


def test_restore_coverage_a_recent_success_stamp_clears_a_stale_join():
    coverage_days = 3 + 5
    seen = {"pvc-1": NOW - (coverage_days + 1) * DAY}
    success = {"pvc-1": str(int(NOW - HOUR))}
    assert (
        logic.check_restore_coverage(["pvc-1", "pvc-2", "pvc-3"], seen, success, NOW, 5)
        is None
    )


def test_restore_drill_oversize_is_clean_when_nothing_is_excluded():
    assert logic.check_restore_drill_oversize([]) is None


def test_restore_drill_oversize_is_flagged_naming_each_excluded_volume():
    problem = logic.check_restore_drill_oversize(
        [("jellyfin-config", 5 * 2**30), ("valheim-config", 4 * 2**30 + 2**29)]
    )
    assert problem == (
        3,
        "volume(s) over the restore-drill actualSize cap, never drilled: "
        "jellyfin-config (5.00 GiB), valheim-config (4.50 GiB)",
    )


def test_restore_drill_nopvc_is_clean_when_nothing_is_excluded():
    assert logic.check_restore_drill_nopvc([]) is None


def test_restore_drill_nopvc_is_flagged_naming_each_excluded_volume():
    problem = logic.check_restore_drill_nopvc(["pvc-a", "pvc-b"])
    assert problem == (
        3,
        "backed-up volume(s) with no bound PVC, never drilled "
        "(rebind, delete, or move to no-backup): pvc-a, pvc-b",
    )
