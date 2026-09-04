#!/usr/bin/env python3
"""Executing tests for the Longhorn backup-plane heartbeat's pure decision core.

Every arm gets an `..._is_clean` / `..._is_flagged` pair (CLAUDE.md's red-proof rule): one input
that must read UP, and one that must read DOWN naming that specific arm — so a guard that stopped
matching (fires on everything, or on nothing) fails its own test rather than reading green
forever. The incidents each threshold exists for are documented in
longhorn_backup_health_logic.py and in longhorn-backup-health.sh.j2's header; this file proves
the ported code still behaves the way those incidents required.

test_reader_pins_the_transport runs longhorn_backup_health.py as a real subprocess against a stub
kubectl, so the I/O layer (env parsing, argv shape, the up/down<TAB>msg contract) is pinned too —
not just the pure functions the rest of this file exercises directly.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_backup_health.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


# ── transport ─────────────────────────────────────────────────────────────────────────────────

READER = Path(__file__).resolve().parents[1] / "files" / "longhorn_backup_health.py"
HOST_LIB_DIR = Path(__file__).resolve().parents[2] / "common" / "files"


def _reader_env(tmp_path, **overrides) -> dict:
    """Every LONGHORN_* env var the reader requires, with permissive defaults a test can override.

    Every one of these is REQUIRED by the reader (`_require_env` et al — no hardcoded fallback,
    the 2026-09-04 review's finding #3), so a subprocess test that used to set only a couple of
    vars and rely on module-level defaults for the rest now has to set all thirteen or the reader
    exits nonzero before doing anything else. Centralised here so each test only names the ONE
    var it cares about overriding. LONGHORN_BACKUP_KUBECTL is the exception: every subprocess
    test points it at its own stub, so it has no default here.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{HOST_LIB_DIR}:{env.get('PYTHONPATH', '')}"
    env["LONGHORN_RESTORE_DRILL_STAMP_DIR"] = str(tmp_path / "no-such-drill-dir")
    env.update(
        {
            "LONGHORN_BACKUP_NAMESPACE": "longhorn-system",
            "LONGHORN_BACKUP_KUBECTL_TIMEOUT_S": "30",
            "LONGHORN_BACKUP_ARMED": "True",
            "LONGHORN_R2_ARMED": "True",
            "LONGHORN_BACKUP_MAX_AGE_HOURS": "30",
            "LONGHORN_WEEKLY_BACKUP_MAX_AGE_HOURS": "198",
            "LONGHORN_BACKUP_ERROR_MAX_AGE_HOURS": "24",
            "LONGHORN_DAILY_BACKUP_BUDGET": "16",
            "LONGHORN_BACKUP_CRON": "30 3 * * *",
            "LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR": "30 4",
            "LONGHORN_RESTORE_DRILL_MAX_AGE_DAYS": "3",
            "LONGHORN_RESTORE_DRILL_COVERAGE_SLACK_DAYS": "5",
        }
    )
    env.update(overrides)
    return env


def test_reader_pins_the_transport(tmp_path):
    """Runs longhorn_backup_health.py as a real subprocess against a stub kubectl.

    The stub fails every call with a distinguishing marker, so this proves the reader shells out
    correctly (LONGHORN_BACKUP_KUBECTL, the namespace flag, env parsing) and still emits the
    up/down<TAB>msg contract the shim depends on — the part no pure-function test can see.

    LONGHORN_BACKUP_KUBECTL is given the stub's ABSOLUTE path rather than a bare name on PATH:
    host_lib.kubectl_runner prepends /usr/local/bin ahead of the caller's PATH, so a same-named
    stub elsewhere on PATH would be shadowed by a real kubectl on a host that has one.
    """
    stub = tmp_path / "stub-kubectl"
    stub.write_text("#!/usr/bin/env bash\necho 'STUB_KUBECTL_MARKER' >&2\nexit 1\n")
    stub.chmod(0o755)

    env = _reader_env(tmp_path, LONGHORN_BACKUP_KUBECTL=str(stub))

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t")
    assert "STUB_KUBECTL_MARKER" in proc.stdout


def test_reader_syslog_line_is_intercepted(tmp_path, logger_calls):
    """The reader's own `logger` call reaches the conftest stub, not the host's syslog.

    This is the non-vacuity half of the autouse `_no_syslog` fixture: an empty `logger_calls`
    would mean either that the reader stopped logging its verdict, or that the real `logger`
    took the call — which is issue #1052, fixture verdicts (`STUB_KUBECTL_MARKER`, pytest tmp
    paths) shipped through Promtail into the Alert History board beside real ones.
    """
    env = _reader_env(tmp_path, LONGHORN_BACKUP_KUBECTL="/bin/false")

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    lines = logger_calls.read_text().splitlines()
    assert any(
        line.startswith("-t longhorn-backup-health status=down") for line in lines
    ), lines


def test_reader_exits_nonzero_naming_a_missing_env_var(tmp_path):
    """A shim that stops exporting a var must be LOUD, not silently fall back to a stale constant.

    Every LONGHORN_* var is required (2026-09-04 review finding #3). This drops one from the
    otherwise-complete env and asserts the reader exits nonzero and names it — which is exactly
    what the shim's `if ! OUT=$(...)` / `[[ $RC -ne 0 ]]` branch turns into a `reader failed`
    push, rather than a wrong-but-plausible verdict computed from a hardcoded fallback.
    """
    env = _reader_env(tmp_path, LONGHORN_BACKUP_KUBECTL="/bin/false")
    del env["LONGHORN_DAILY_BACKUP_BUDGET"]

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "LONGHORN_DAILY_BACKUP_BUDGET" in proc.stderr


def _rfc3339(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _green_path_stub_kubectl(tmp_path, snapshot_ts: str) -> Path:
    """A stub kubectl answering every query the reader's green path issues, from fixtures.

    Dispatches on argv (after stripping the `-n <namespace>` host_lib.kubectl_runner inserts),
    not on raw text matching, so it stays exact even though several distinct queries all target
    `backups.longhorn.io`/`volumes.longhorn.io` with different -o jsonpath shapes. One volume,
    `pvc-web-data`, is backed up by the "daily" tier only — every other tier's label selector
    matches nothing, which is the ordinary (and simplest-to-fixture) shape for a fleet where only
    one recurring job is armed.

    Each dispatch arm carries a branch NAME, and two env knobs turn one named branch red without
    disturbing the other eight: `STUB_FAIL_BRANCH` makes it exit 124 (host_lib's timeout code)
    and `STUB_NULL_BRANCH` makes it answer the JSON literal `null` with rc 0. That is what lets
    the fetch-failure tests below reuse this one fixture instead of shipping a stub per fetch.
    """
    stub = tmp_path / "stub-kubectl-green"
    script = r"""#!/usr/bin/env python3
import os
import sys

SNAPSHOT_TS = "__SNAPSHOT_TS__"

args = sys.argv[1:]
if "-n" in args:
    i = args.index("-n")
    args = args[:i] + args[i + 2:]
joined = " ".join(args)

if args[:2] == ["get", "backuptarget"]:
    branch, body = "target-" + args[2], "true"
elif args[:2] == ["get", "backups.longhorn.io"] and args[-1] == "json":
    branch, body = "errored-backups", '{"items": []}'
elif args[:2] == ["get", "jobs.batch"] and args[-1] == "json":
    branch, body = "failed-jobs", '{"items": []}'
elif args[:2] == ["get", "backups.longhorn.io"] and "|" in joined:
    branch = "coverage"
    body = "pvc-web-data|%s|daily-backup\n" % SNAPSHOT_TS
elif args[:2] == ["get", "backups.longhorn.io"] and "size" in joined:
    branch = "recent"
    body = "pvc-web-data %s 1048576\n" % SNAPSHOT_TS
elif args[:2] == ["get", "backups.longhorn.io"] and "snapshotCreatedAt" in joined:
    branch, body = "freshness", "%s\n" % SNAPSHOT_TS
elif args[:2] == ["get", "volumes.longhorn.io"] and "-l" in args:
    sel = args[args.index("-l") + 1]
    branch = "tier-" + sel.split("/")[-1].split("=")[0]
    if sel == "recurring-job-group.longhorn.io/default=enabled":
        body = "pvc-web-data %s default/web-data default\n" % SNAPSHOT_TS
    else:
        body = ""
elif args[:2] == ["get", "volumes.longhorn.io"]:
    branch, body = "r2", ""
else:
    sys.stderr.write("UNEXPECTED ARGS: %r\n" % (args,))
    sys.exit(1)

if branch == os.environ.get("STUB_FAIL_BRANCH"):
    sys.stderr.write("STUB_FETCH_FAILED %s\n" % branch)
    sys.exit(124)
if branch == os.environ.get("STUB_NULL_BRANCH"):
    body = "null"

sys.stdout.write(body)
""".replace("__SNAPSHOT_TS__", snapshot_ts)
    stub.write_text(script)
    stub.chmod(0o755)
    return stub


def test_reader_green_path_pins_the_transport(tmp_path):
    """The clean half of test_reader_pins_the_transport: every query answered, verdict is UP.

    The red-path test above stubs kubectl to fail every call, which exercises the shell-out and
    the down<TAB>msg contract but never the eight checks' happy path — the jsonpath literals,
    the three row parsers, or the up<TAB>msg contract the shim's success branch depends on. This
    runs the reader against fixtures shaped to leave every one of the eight checks clean.
    """
    now = time.time()
    snapshot_ts = _rfc3339(now - 60)
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(now - 3600)))

    stub = _green_path_stub_kubectl(tmp_path, snapshot_ts)
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
    )

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("up\t"), proc.stdout
    assert "backup target(s) default r2 available" in proc.stdout
    assert "1 backed-up volume(s) covered across daily+weekly" in proc.stdout
    assert "1 B2 backup(s)/24h (budget 16)" in proc.stdout


# ── fetch failures: the deadman must go DOWN with a reason, never quietly UP (issue #1061) ────
#
# test_reader_green_path_pins_the_transport above is the CLEAN half of every pair below: the same
# fixture, no knob set, verdict UP. Each test here turns exactly one fetch red and asserts the
# verdict flips and names that fetch — so a helper that stopped appending its problem, or one
# that started firing on a healthy answer, fails a test rather than reading green forever.
#
# The reader's OWN syslog line carries the full unranked problem list; stdout carries only the
# top-ranked one plus a count, so the fetch name is asserted against `logger_calls`.


def _run_reader_against(stub, tmp_path, **env_overrides):
    """Run the reader against `stub` on an otherwise-green fixture, returning the finished proc."""
    now = time.time()
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir(exist_ok=True)
    (drill_dir / "last-success").write_text(str(int(now - 3600)))
    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        **env_overrides,
    )
    return subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("branch", "named"),
    [
        ("freshness", "backup freshness fetch failed (rc=124)"),
        ("errored-backups", "errored-backups fetch failed (rc=124)"),
        ("coverage", "backup coverage fetch failed (rc=124)"),
        ("tier-default", "daily tier volumes fetch failed (rc=124)"),
        ("recent", "recent backups fetch failed (rc=124)"),
        ("r2", "r2 volume set fetch failed (rc=124)"),
        ("failed-jobs", "failed-jobs fetch failed (rc=124)"),
    ],
)
def test_a_timed_out_fetch_is_flagged_by_name(tmp_path, logger_calls, branch, named):
    """rc 124 is host_lib's timeout code — the exact case that used to read as an empty result.

    Every one of these seven fetches turned a nonzero rc into `[]`/`set()` and fed it to a check
    that reads empty as clean: "nothing errored", "no failed jobs", or a tier silently dropped
    from the coverage count. A 30s API-server timeout on one call therefore left the whole
    verdict UP with a quietly smaller number in it.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(time.time() - 60))
    proc = _run_reader_against(stub, tmp_path, STUB_FAIL_BRANCH=branch)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t"), proc.stdout
    logged = logger_calls.read_text()
    assert named in logged, logged


@pytest.mark.parametrize(
    ("branch", "named"),
    [
        ("errored-backups", "errored-backups fetch returned an unparseable body"),
        ("failed-jobs", "failed-jobs fetch returned an unparseable body"),
    ],
)
def test_an_unparseable_json_body_is_flagged_by_name(
    tmp_path, logger_calls, branch, named
):
    """A `null` body parses cleanly and carries no `items` — kubectl's answer on a truncated read.

    `json.loads("null")` returns None rather than raising, so the reader's old `except ValueError`
    never saw this one: it reached `.get("items")` as an AttributeError. Only the two `-o json`
    fetches are covered — for the five jsonpath fetches a garbage body is indistinguishable from
    data, and a malformed jsonpath makes kubectl exit nonzero, which the rc pair above covers.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(time.time() - 60))
    proc = _run_reader_against(stub, tmp_path, STUB_NULL_BRANCH=branch)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t"), proc.stdout
    logged = logger_calls.read_text()
    assert named in logged, logged


def test_a_failed_coverage_fetch_does_not_cascade_into_the_tier_loop(
    tmp_path, logger_calls
):
    """One unread fetch reports one problem, not ten.

    Every tier is matched against the coverage rows, so passing the loop an empty list on a
    failed coverage fetch would report all nine tiers' volumes as stale or missing — burying the
    one thing that actually happened under nine consequences of it.
    """
    stub = _green_path_stub_kubectl(tmp_path, _rfc3339(time.time() - 60))
    proc = _run_reader_against(stub, tmp_path, STUB_FAIL_BRANCH="coverage")

    assert proc.stdout.startswith("down\t"), proc.stdout
    logged = logger_calls.read_text()
    assert "backup coverage fetch failed" in logged, logged
    assert "tier volumes fetch failed" not in logged, logged
    assert "stale or missing" not in logged, logged


def _grace_pair_stub_kubectl(tmp_path, created_ts: str, old_backup_ts: str) -> Path:
    """Two daily-tier volumes: `pvc-old` already backed up, `pvc-new` created moments ago.

    `pvc-old` has a matching coverage row so checks 2/3/5/6 stay clean regardless of the cron
    parse — only `pvc-new`'s fate (graced silently vs. paged as uncovered) depends on whether
    LONGHORN_BACKUP_CRON parses.
    """
    stub = tmp_path / "stub-kubectl-grace"
    script = f"""#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if "-n" in args:
    i = args.index("-n")
    args = args[:i] + args[i + 2:]
joined = " ".join(args)


def emit(text, rc=0):
    sys.stdout.write(text)
    sys.exit(rc)


if args[:3] == ["get", "backuptarget", "default"]:
    emit("true")
elif args[:2] == ["get", "backups.longhorn.io"] and args[-1] == "json":
    emit('{{"items": []}}')
elif args[:2] == ["get", "jobs.batch"] and args[-1] == "json":
    emit('{{"items": []}}')
elif args[:2] == ["get", "backups.longhorn.io"] and "|" in joined:
    emit("pvc-old|{old_backup_ts}|daily-backup\\n")
elif args[:2] == ["get", "backups.longhorn.io"] and "size" in joined:
    emit("pvc-old {old_backup_ts} 1048576\\n")
elif args[:2] == ["get", "backups.longhorn.io"] and "snapshotCreatedAt" in joined:
    emit("{old_backup_ts}\\n")
elif args[:2] == ["get", "volumes.longhorn.io"] and "-l" in args:
    sel = args[args.index("-l") + 1]
    if sel == "recurring-job-group.longhorn.io/default=enabled":
        emit(
            "pvc-old {old_backup_ts} default/old-data default\\n"
            "pvc-new {created_ts} default/new-data default\\n"
        )
    else:
        emit("")
elif args[:2] == ["get", "volumes.longhorn.io"]:
    emit("")
else:
    sys.stderr.write("UNEXPECTED ARGS: %r\\n" % (args,))
    sys.exit(1)
"""
    stub.write_text(script)
    stub.chmod(0o755)
    return stub


def test_malformed_cron_pages_the_new_volume_instead_of_gracing_it(tmp_path):
    """FLAGGED half: a malformed LONGHORN_BACKUP_CRON used to raise IndexError before main() ran.

    _hhmm_from_two_field_cron() executed at module scope, so a bad value took down all eight
    checks at once, not just the daily tier's first-run grace (2026-09-04 review finding #4).
    With the fix, a malformed value degrades to `first_run_after(..., None, ...)`, which
    check_tier() already treats as "no grace" — `pvc-new`, created moments ago, is paged as
    uncovered instead of silently excused, and the reader still completes and emits a verdict.
    """
    now = time.time()
    old_ts = _rfc3339(now - 3600)
    new_ts = _rfc3339(now - 30)
    stub = _grace_pair_stub_kubectl(tmp_path, created_ts=new_ts, old_backup_ts=old_ts)
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(now - 3600)))

    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_R2_ARMED="False",
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        LONGHORN_BACKUP_CRON="30",  # malformed: one field, no hour
    )

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("down\t"), proc.stdout
    assert "pvc-new" in proc.stdout or "new-data" in proc.stdout, proc.stdout


def test_well_formed_cron_graces_the_new_volume(tmp_path):
    """CLEAN half of the malformed-cron pair: a well-formed value grants the new-volume grace."""
    now = time.time()
    old_ts = _rfc3339(now - 3600)
    new_ts = _rfc3339(now - 30)
    stub = _grace_pair_stub_kubectl(tmp_path, created_ts=new_ts, old_backup_ts=old_ts)
    drill_dir = tmp_path / "drill"
    drill_dir.mkdir()
    (drill_dir / "last-success").write_text(str(int(now - 3600)))

    env = _reader_env(
        tmp_path,
        LONGHORN_BACKUP_KUBECTL=str(stub),
        LONGHORN_R2_ARMED="False",
        LONGHORN_RESTORE_DRILL_STAMP_DIR=str(drill_dir),
        LONGHORN_BACKUP_CRON="30 3 * * *",
    )

    proc = subprocess.run(
        [sys.executable, str(READER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("up\t"), proc.stdout
    assert "awaiting their first scheduled backup" in proc.stdout, proc.stdout
