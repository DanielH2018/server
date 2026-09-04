#!/usr/bin/env python3
"""Executing tests for the Longhorn backup-plane heartbeat's pure decision core.

Every arm gets an `..._is_clean` / `..._is_flagged` pair (CLAUDE.md's red-proof rule): one input
that must read UP, and one that must read DOWN naming that specific arm — so a guard that stopped
matching (fires on everything, or on nothing) fails its own test rather than reading green
forever. The incidents each threshold exists for are documented in
longhorn_backup_health_logic.py and in longhorn-backup-health.sh.j2's header.

The I/O layer is pinned by real subprocess runs in `test_longhorn_backup_health_reader.py` and
`test_longhorn_backup_grace_cron.py`, which share `_longhorn_reader_stubs.py`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_backup_health.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
import longhorn_backup_health_logic as logic

HOUR = 3600.0
DAY = 86400.0
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z-ish; fixed so every test is deterministic.


# ── check 1: backup target availability ──────────────────────────────────────────────────────


def test_target_availability_is_clean_when_all_armed_targets_report_true():
    problems = logic.check_target_availability(
        {"default": {"raw": "true", "reason": ""}}
    )
    assert problems == []


def test_target_availability_is_flagged_when_a_target_is_unavailable():
    problems = logic.check_target_availability(
        {"default": {"raw": "false", "reason": "backup target URL is empty"}}
    )
    assert problems == [
        (1, "backup target default unavailable: backup target URL is empty")
    ]


def test_target_availability_falls_back_to_raw_when_no_reason():
    """The 2026-08-02 outage: kubectl itself failed and there was no condition message to read."""
    problems = logic.check_target_availability(
        {"default": {"raw": "Error from server: etcdserver timeout", "reason": ""}}
    )
    assert problems == [
        (1, "backup target default unavailable: Error from server: etcdserver timeout")
    ]


# ── check 2: freshness ────────────────────────────────────────────────────────────────────────


def test_freshness_is_clean_when_the_newest_backup_is_within_bounds():
    problem, age_s = logic.check_freshness("2027-01-15T06:00:00Z", NOW, 30 * HOUR, 30)
    assert problem is None
    assert age_s == HOUR * 2


def test_freshness_is_flagged_when_no_backups_exist():
    problem, _age_s = logic.check_freshness(None, NOW, 30 * HOUR, 30)
    assert problem == (1, "no backups exist")


def test_freshness_is_flagged_when_the_newest_backup_is_too_old():
    old = "2027-01-13T08:00:00Z"  # 48h before NOW
    problem, _age_s = logic.check_freshness(old, NOW, 30 * HOUR, 30)
    assert problem == (1, "newest backup is 48h old (limit 30h)")


def test_freshness_is_flagged_on_an_unparseable_timestamp():
    problem, _ = logic.check_freshness("not-a-timestamp", NOW, 30 * HOUR, 30)
    assert problem == (1, "unparseable backup timestamp: not-a-timestamp")


# ── rfc3339_to_epoch: `.status.snapshotCreatedAt` is a plain string, not a metav1.Time ─────────
#
# Unlike `.metadata.creationTimestamp`, Longhorn writes snapshotCreatedAt itself with no format
# guarantee. `date -d` (what the original shell used) accepts fractional seconds and a numeric
# UTC offset; a parser that doesn't page a false RED on check 2/4 the first time a sub-second
# stamp shows up.


def test_rfc3339_to_epoch_accepts_fractional_seconds():
    assert logic.rfc3339_to_epoch("2027-01-15T08:00:00.123456789Z") == NOW


def test_rfc3339_to_epoch_accepts_a_numeric_utc_offset():
    assert logic.rfc3339_to_epoch("2027-01-15T08:00:00+00:00") == NOW


def test_rfc3339_to_epoch_still_rejects_garbage():
    assert logic.rfc3339_to_epoch("not-a-timestamp") is None
    assert logic.rfc3339_to_epoch("") is None


# ── check 3: errored backups ─────────────────────────────────────────────────────────────────


def _backup(state, created, name="backup-abc123", vol="pvc-1"):
    return {
        "status": {"state": state},
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"backup-volume": vol},
        },
    }


def test_errored_backups_is_clean_when_nothing_errored():
    assert (
        logic.check_errored_backups(
            [_backup("Completed", "2027-01-15T07:00:00Z")], NOW - 24 * HOUR, 24
        )
        is None
    )


def test_errored_backups_is_flagged_within_the_age_window():
    """daily-backup-29775090's failure at 03:30 on 2026-08-12 — this is the check it fed."""
    problem = logic.check_errored_backups(
        [_backup("Error", "2027-01-15T07:00:00Z")], NOW - 24 * HOUR, 24
    )
    assert problem == (2, "backups that failed in the last 24h: backup-abc123 (pvc-1)")


def test_errored_backups_self_clears_once_the_object_ages_out():
    """The 11-immortal-Error-objects incident: unbounded, this would page forever."""
    old = "2027-01-01T00:00:00Z"
    assert (
        logic.check_errored_backups([_backup("Error", old)], NOW - 24 * HOUR, 24)
        is None
    )


# ── check 4: per-tier coverage ───────────────────────────────────────────────────────────────


def test_coverage_is_clean_when_the_volume_has_a_fresh_tier_backup():
    result = logic.TierResult()
    rows = [("pvc-1", "2027-01-01T00:00:00Z", "ns/claim1", "default")]
    coverage = [("pvc-1", "2027-01-15T06:00:00Z", "daily-backup")]
    logic.check_tier(
        result,
        rows,
        coverage,
        30 * HOUR,
        "daily",
        "daily-backup",
        "3:30",
        "*",
        set(),
        NOW,
    )
    assert logic.uncovered_problem(result) is None
    assert result.checked == 1


def test_coverage_is_flagged_when_the_tiers_own_job_has_gone_stale():
    """2026-08-16: matching by (volume, job) is what makes a dead TIER visible.

    A daily backup exists for pvc-1, but nothing from `weekly-backup-d0` ever landed — a
    volume-only match would have borrowed the daily evidence and stayed green.
    """
    result = logic.TierResult()
    rows = [("pvc-1", "2027-01-01T00:00:00Z", "ns/claim1", "default")]
    coverage = [("pvc-1", "2027-01-15T06:00:00Z", "daily-backup")]
    logic.check_tier(
        result,
        rows,
        coverage,
        198 * HOUR,
        "weekly-d0",
        "weekly-backup-d0",
        "4:30",
        "0",
        set(),
        NOW,
    )
    problem = logic.uncovered_problem(result)
    assert problem is not None
    assert problem[0] == 3
    assert "ns/claim1 (weekly-d0, no backup from weekly-backup-d0)" in problem[1]


def test_coverage_suppresses_a_disarmed_targets_volume():
    result = logic.TierResult()
    rows = [("pvc-1", "2027-01-01T00:00:00Z", "ns/claim1", "r2")]
    logic.check_tier(
        result, rows, [], 30 * HOUR, "daily", "daily-backup", "3:30", "*", {"r2"}, NOW
    )
    assert logic.uncovered_problem(result) is None
    assert result.suppressed == 1


def test_coverage_grace_counts_a_new_volume_rather_than_dropping_it_silently():
    """2026-08-20: the grace branch's bare `continue` left 14 of 25 volumes invisible."""
    result = logic.TierResult()
    created = (
        "2027-01-15T07:50:00Z"  # 10 minutes before NOW; first 3:30 run is still ahead
    )
    rows = [("pvc-1", created, "ns/claim1", "default")]
    logic.check_tier(
        result, rows, [], 30 * HOUR, "daily", "daily-backup", "9:00", "*", set(), NOW
    )
    assert result.graced == 1
    assert result.graced_vols == ["ns/claim1"]
    assert result.checked == 0, "a graced volume must never count as covered"
    assert logic.uncovered_problem(result) is None


def test_coverage_grace_distinguishes_seeded_from_brand_new():
    result = logic.TierResult()
    created = "2027-01-15T07:50:00Z"
    rows = [("pvc-1", created, "ns/claim1", "default")]
    # A backup exists, just not from THIS tier's job — the volume is seeded, not offsite-empty.
    coverage = [("pvc-1", "2027-01-10T00:00:00Z", "some-other-job")]
    logic.check_tier(
        result,
        rows,
        coverage,
        30 * HOUR,
        "daily",
        "daily-backup",
        "9:00",
        "*",
        set(),
        NOW,
    )
    assert result.graced_seeded == 1
    assert result.graced_new == 0


# ── check 5: recent-backup (B2 transaction) budget ───────────────────────────────────────────


def test_recent_budget_is_clean_within_budget():
    rows = [("pvc-1", "2027-01-15T07:00:00Z", "1048576")]
    n, total = logic.compute_recent_backups(rows, set(), NOW - DAY)
    assert logic.check_recent_budget(n, total, 16) is None


def test_recent_budget_is_flagged_over_budget():
    rows = [("pvc-%d" % i, "2027-01-15T07:00:00Z", "1048576") for i in range(17)]
    n, total = logic.compute_recent_backups(rows, set(), NOW - DAY)
    problem = logic.check_recent_budget(n, total, 16)
    assert problem is not None
    assert problem[0] == 4
    assert "17 backups in 24h exceeds the 16 budget" in problem[1]


def test_recent_budget_excludes_r2_routed_volumes():
    """R2 spends Cloudflare's allowance, not B2's — counting it inflates a B2-shaped budget."""
    rows = [("pvc-r2", "2027-01-15T07:00:00Z", "1048576")] * 20
    n, _ = logic.compute_recent_backups(rows, {"pvc-r2"}, NOW - DAY)
    assert n == 0


def test_recent_budget_tolerates_a_non_numeric_size():
    """Mirrors `$(( RECENT_BYTES + ${SIZE:-0} ))`: bash arithmetic treats junk as 0, not a crash.

    `.status.size` is a plain string Longhorn writes with no format guarantee — a reader that
    raises on it takes down every OTHER check in the same tick, which is strictly worse than the
    one backup this size belongs to going uncounted.
    """
    rows = [("pvc-1", "2027-01-15T07:00:00Z", "not-a-number")]
    n, total = logic.compute_recent_backups(rows, set(), NOW - DAY)
    assert n == 1
    assert total == 0


# ── check 6: failed Jobs ─────────────────────────────────────────────────────────────────────


def _job(name, failed_at=None, created="2027-01-01T00:00:00Z"):
    status = {"conditions": []}
    if failed_at:
        status["conditions"] = [
            {"type": "Failed", "status": "True", "lastTransitionTime": failed_at}
        ]
    return {"metadata": {"name": name, "creationTimestamp": created}, "status": status}


def test_failed_jobs_is_clean_when_nothing_failed():
    assert (
        logic.check_failed_jobs([_job("daily-backup-1")], NOW - 24 * HOUR, 24) is None
    )


def test_failed_jobs_is_flagged_within_the_window():
    problem = logic.check_failed_jobs(
        [_job("daily-backup-29775090", failed_at="2027-01-15T07:00:00Z")],
        NOW - 24 * HOUR,
        24,
    )
    assert problem == (
        2,
        "backup job(s) that failed in the last 24h: daily-backup-29775090",
    )


def test_failed_jobs_is_dated_by_the_condition_not_creation():
    """An 8h retry window separates the two on the run this check was written for."""
    problem = logic.check_failed_jobs(
        [
            _job(
                "daily-backup-29775090",
                created="2027-01-15T00:00:00Z",  # outside a 6h-ago cutoff on its own
                failed_at="2027-01-15T07:00:00Z",  # inside it
            )
        ],
        NOW - 6 * HOUR,
        24,
    )
    assert problem is not None


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


# ── final verdict assembly ───────────────────────────────────────────────────────────────────


def test_build_verdict_is_up_with_no_problems_and_notes_disarmed_and_graced_state():
    status, msg, push_msg = logic.build_verdict(
        [],
        backup_targets=["default"],
        disarmed_targets=["r2"],
        age_s=2 * HOUR,
        checked=10,
        recent_n=3,
        daily_backup_budget=16,
        suppressed=4,
        graced_new=1,
        graced_new_vols=["ns/new"],
        graced_seeded=2,
        graced_seeded_vols=["ns/a", "ns/b"],
    )
    assert status == "up"
    assert msg == push_msg
    assert "DISARMED: r2 (4 volume(s) not checked)" in msg
    assert "1 volume(s) awaiting their first scheduled backup" in msg
    assert "2 volume(s) seeded but not yet in rotation" in msg


def test_build_verdict_ranks_the_pushed_message_and_keeps_the_full_one_in_msg():
    problems = [(3, "stale volumes"), (1, "target unavailable"), (2, "backup failed")]
    status, msg, push_msg = logic.build_verdict(
        problems,
        backup_targets=["default"],
        disarmed_targets=[],
        age_s=0,
        checked=0,
        recent_n=0,
        daily_backup_budget=16,
        suppressed=0,
        graced_new=0,
        graced_new_vols=[],
        graced_seeded=0,
        graced_seeded_vols=[],
    )
    assert status == "down"
    assert msg == "stale volumes; target unavailable; backup failed"
    assert (
        push_msg
        == "target unavailable (+2 more: see journalctl -t longhorn-backup-health)"
    )


def test_build_verdict_ties_break_by_encounter_order():
    problems = [(1, "first rank-1"), (1, "second rank-1")]
    _, _, push_msg = logic.build_verdict(
        problems,
        backup_targets=[],
        disarmed_targets=[],
        age_s=0,
        checked=0,
        recent_n=0,
        daily_backup_budget=16,
        suppressed=0,
        graced_new=0,
        graced_new_vols=[],
        graced_seeded=0,
        graced_seeded_vols=[],
    )
    assert push_msg.startswith("first rank-1")
