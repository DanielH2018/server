#!/usr/bin/env python3
"""Executing tests for checks 9 and 10 — the trim's and the B2 accounting's evidence plane.

Check 9 reads what each job SAID; check 10 reads whether it ran at all. Both live in
`longhorn_cron_evidence_logic.py`, split from `longhorn_backup_health_logic.py` when it reached
its module-length cap.

The trim is Longhorn's `trim` RecurringJob, so its fixtures are a CronJob, Jobs and a pod log.
The pod-log lines are copied from a live run of longhorn-manager v1.13.0 and matched as written:
a reword upstream fails these rather than silently blinding the check. The B2 accounting is a
host cron, so its fixtures are journal lines.

The B2 transport is pinned separately by a real subprocess run in
`test_longhorn_backup_health_reader.py::test_reader_pins_the_journal_transport`.

Run: uv run pytest scripts/lib/tests/test_longhorn_backup_health_cron_evidence.py
"""

from longhorn_lib import longhorn_cron_evidence_logic as logic
from _shell_render import rendered_shell_text
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, K3S_ROLE

_NOW = 1_700_000_000.0


def _ts(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _cronjob(schedule="10 6 * * *", last_s=_NOW - 3600, suspend=False):
    status = {"lastScheduleTime": _ts(last_s)} if last_s is not None else {}
    return {
        "metadata": {"name": "trim", "creationTimestamp": _ts(_NOW - 30 * 86400)},
        "spec": {"schedule": schedule, "suspend": suspend},
        "status": status,
    }


def _job(name, created_s, failed=False, label="trim"):
    condition = {"type": "Failed" if failed else "Complete", "status": "True"}
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": _ts(created_s),
            "labels": {"recurring-job.longhorn.io": label},
        },
        "status": {"conditions": [condition]},
    }


_JOBS = [_job("trim-29893330", _NOW - 3600)]
# A healthy run's first line names a failure Longhorn retries past; it is not a trim failure.
_POD_CLEAN = (
    '[pod/trim-29893330-f2mm2/trim] time="2026-11-02T06:10:00Z" level=warning '
    'msg="Failed to create Longhorn manager API client, retrying" func=recurringjob.newAPIClient\n'
    '[pod/trim-29893330-f2mm2/trim] time="2026-11-02T06:10:06Z" level=info '
    'msg="Finished recurring filesystem trim" job=trim\n'
)
# One volume's failure as longhorn-manager v1.13.0 writes it: the volume job's error, which
# carries `volumeName=`, then `startJob`'s warning, which carries `volume=`.
_POD_FAILED = _POD_CLEAN + (
    '[pod/trim-29893330-f2mm2/trim] time="2026-11-02T06:11:00Z" level=error '
    'msg="Failed to run volume job" error="failed to complete filesystem-trim for '
    'pvc-0123: timeout" job=trim task=filesystem-trim volumeName=pvc-0123\n'
    '[pod/trim-29893330-f2mm2/trim] time="2026-11-02T06:11:00Z" level=warning '
    'msg="Failed to run recurring job for volume" error="failed to complete '
    'filesystem-trim for pvc-0123: timeout" job=trim volume=pvc-0123\n'
)


def test_trim_evidence_is_clean_on_a_clean_run():
    assert logic.check_trim(_cronjob(), _JOBS, _POD_CLEAN, _NOW) == []


def test_cron_evidence_is_flagged_on_a_failed_trim():
    """A per-volume failure leaves the Job succeeded, so only the pod log can report it."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    problems = logic.check_trim(_cronjob(), _JOBS, _POD_FAILED, _NOW)
    assert problems == [
        (
            4,
            "the trim job failed on 1 volume(s) on its last run — freed blocks are still "
            "being backed up: pvc-0123",
        )
    ]


def test_trim_evidence_names_the_volume_from_each_failure_shape():
    """Each of Longhorn's failure lines names its volume a different way; each one counts once."""
    prefix = '[pod/trim-29893330-f2mm2/trim] time="2026-11-02T06:12:00Z" level=error '
    log = _POD_FAILED + (
        prefix
        + 'msg="Failed to initialize job for volume pvc-0456" error="not found" job=trim\n'
        + prefix
        + 'msg="Failed to run volume job" error="timeout" job=trim volumeName=pvc-0789\n'
    )
    problems = logic.check_trim(_cronjob(), _JOBS, log, _NOW)
    assert len(problems) == 1
    assert problems[0][1].endswith(
        "failed on 3 volume(s) on its last run — freed blocks are "
        "still being backed up: pvc-0123, pvc-0456, pvc-0789"
    )


def test_trim_evidence_reads_only_the_newest_jobs_pod():
    """A failed Job's pod can outlive it beside a newer clean run, and is not today's verdict."""
    old = _POD_FAILED.replace("trim-29893330-f2mm2", "trim-29891890-aaaaa")
    jobs = _JOBS + [_job("trim-29891890", _NOW - 86400 - 3600)]
    assert logic.check_trim(_cronjob(), jobs, _POD_CLEAN + old, _NOW) == []


def test_trim_evidence_ignores_another_jobs_failure_line():
    log = _POD_FAILED.replace("/trim] ", "/daily-backup] ").replace(
        "pod/trim-29893330", "pod/daily-backup-1"
    )
    assert logic.check_trim(_cronjob(), _JOBS, log, _NOW) == []


def test_trim_evidence_is_flagged_on_a_failed_job():
    jobs = [_job("trim-29893330", _NOW - 3600, failed=True)]
    problems = logic.check_trim(_cronjob(), jobs, "", _NOW)
    assert [msg for _, msg in problems] == [
        "the trim Job trim-29893330 failed — see its pod log in longhorn-system"
    ]


def test_trim_is_flagged_when_its_cronjob_is_gone():
    """The RecurringJob deleted, or Longhorn not reconciling it: no trim is scheduled."""
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract (the crons that change state with no human in the loop)
    problems = logic.check_trim(None, _JOBS, _POD_CLEAN, _NOW)
    assert problems == [
        (
            4,
            "the trim RecurringJob has no CronJob, so Longhorn schedules no filesystem trim",
        )
    ]


def test_trim_liveness_is_flagged_past_one_period_of_the_schedule():
    problems = logic.check_trim(_cronjob(last_s=_NOW - 27 * 3600), _JOBS, "", _NOW)
    assert [msg for _, msg in problems] == [
        "the trim CronJob (10 6 * * *) last started a Job 27h ago — it has stopped running"
    ]


def test_trim_liveness_reads_the_period_from_the_schedule():
    """A weekly schedule allows a week of silence, read from the CronJob, not a baked key."""
    assert (
        logic.check_trim(
            _cronjob("10 6 * * 1", last_s=_NOW - 6 * 86400), _JOBS, "", _NOW
        )
        == []
    )


def test_trim_liveness_is_flagged_on_a_schedule_it_cannot_judge():
    problems = logic.check_trim(_cronjob("*/5 6 * * *"), _JOBS, "", _NOW)
    assert len(problems) == 1 and "cannot be judged" in problems[0][1]


def test_trim_liveness_is_flagged_on_a_suspended_cronjob():
    problems = logic.check_trim(_cronjob(suspend=True), _JOBS, "", _NOW)
    assert [msg for _, msg in problems] == [
        "the trim CronJob is suspended, so no filesystem trim runs"
    ]


def test_trim_evidence_skips_the_arms_whose_read_failed():
    """The caller reports the failed Job or pod-log read itself; one fault is one problem."""
    assert logic.check_trim(_cronjob(), None, _POD_FAILED, _NOW) == []
    assert logic.check_trim(_cronjob(), _JOBS, None, _NOW) == []


# ── the B2 deletion accounting ─────────────────────────────────────────────────────────────

# The one line a COMPLETED `b2-deletions` run always ends with, copied from
# `deletions_summary_line` in `scripts/diagnostics/probe_lib/b2_ledger.py` and matched as
# written.
_SUMMARY = "b2-deletions: charged 3, skipped 0, unpriced 0"


def test_cron_evidence_is_clean_on_a_priced_accounting():
    assert logic.check_deletion_evidence([_SUMMARY], 26) == []


def test_cron_evidence_is_flagged_on_an_unpriced_deletion_anywhere_in_the_window():
    """`logger` writes one entry per line, so the UNPRICED block is not the newest line."""
    lines = [
        "charged 1 deletion(s) over 26h: 12 Class C",
        "UNPRICED — these deletions spent Class C that cannot now be recovered:",
        "  n8n-files                backup-abc123",
        "  Their volume is absent from the block-tree snapshot, so nothing prices them.",
    ]
    problems = logic.check_deletion_evidence(lines, 26)
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "UNPRICED" in problems[0][1]


def test_cron_evidence_reports_a_journal_read_that_failed():
    """None is "I could not look", which is not the same answer as an empty window."""
    problems = logic.check_deletion_evidence(None, 26)
    assert [rank for rank, _ in problems] == [4]
    assert "b2-deletions journal" in problems[0][1]


def test_cron_evidence_is_silent_when_the_window_holds_nothing():
    """No reading is not a verdict. Whether the cron still runs at all is a separate alarm."""
    assert logic.check_deletion_evidence([], 26) == []


# ── check 10: is the B2 cron still firing at all? ───────────────────────────────────────────

# Older than the 26h window, so a cron installed then has had a full window to fire in.
_INSTALLED_LONG_AGO = _NOW - 30 * 3600
_B2 = logic.CronState(
    "b2-deletions",
    "/etc/cron.d/b2-deletion-accounting",
    _INSTALLED_LONG_AGO,
    False,
    True,
)


def _liveness(lines, deletion=_B2, window=26):
    problem = logic.check_deletion_liveness(lines, deletion, window, _NOW)
    return [problem] if problem else []


def test_cron_liveness_is_clean_when_the_cron_spoke_in_the_window():
    assert _liveness([_SUMMARY]) == []


def test_cron_liveness_is_flagged_when_a_long_installed_cron_says_nothing():
    """The gap check 9 leaves open: an empty window reads as green to the content arm."""
    problems = _liveness([])
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "b2-deletions has logged nothing in the last 26h" in problems[0][1]
    assert "/etc/cron.d/b2-deletion-accounting" in problems[0][1]


def test_cron_liveness_is_flagged_when_the_cron_entry_is_gone():
    """The issue's verify-by: remove the entry and the tile names the silent cron."""
    problems = _liveness([], deletion=_B2._replace(installed_at=None))
    assert len(problems) == 1
    assert "the b2-deletions cron is not installed" in problems[0][1]


def test_cron_liveness_is_clean_on_a_freshly_provisioned_host():
    """Installed inside the window, so it has not yet had a full period to fire in."""
    assert _liveness([], deletion=_B2._replace(installed_at=_NOW - 3600)) == []


def test_cron_liveness_is_clean_for_a_cron_this_host_does_not_install():
    """`has_repo_checkout: false` never gets the B2-accounting cron — it must not page."""
    absent = _B2._replace(installed_at=None, expected=False)
    assert _liveness([], deletion=absent) == []


def test_cron_liveness_separates_a_missing_cron_from_one_it_cannot_stat():
    """A missing cron and one it cannot stat have different fixes."""
    blind = _B2._replace(installed_at=None, unreadable=True)
    problems = _liveness([], deletion=blind)
    assert len(problems) == 1
    assert "could not stat /etc/cron.d/b2-deletion-accounting" in problems[0][1]
    assert "not installed" not in problems[0][1]


def test_cron_liveness_names_a_cron_that_fired_and_logged_no_completed_run():
    """The 2026-10-08 case: /etc/cron.d is root-only, and the cron fired but only refused (#3677)."""
    fired = _B2._replace(
        installed_at=None,
        unreadable=True,
        fired_stamp="/var/lib/longhorn-cron-fired/b2-deletion-accounting",
        fired_at=_NOW - 2 * 3600,
    )
    problems = _liveness(["cannot confirm this kubectl ... Refusing"], deletion=fired)
    assert len(problems) == 1
    assert "b2-deletions fired 2h ago but logged no completed run" in problems[0][1]
    assert "could not stat" not in problems[0][1]


def test_cron_liveness_reads_a_stale_fire_stamp_as_not_firing():
    """A stamp older than the window is no evidence; the message names both readers."""
    stale = _B2._replace(
        installed_at=None,
        unreadable=True,
        fired_stamp="/var/lib/longhorn-cron-fired/b2-deletion-accounting",
        fired_at=_NOW - 30 * 3600,
    )
    problems = _liveness([], deletion=stale)
    assert len(problems) == 1
    assert "b2-deletions has not fired in the last 26h" in problems[0][1]
    assert "/var/lib/longhorn-cron-fired/b2-deletion-accounting" in problems[0][1]


def test_the_cron_touches_the_stamp_the_shim_exports():
    """The cron writes the stamp and the shim names it; nothing else holds the two paths together."""
    stamp_dir = yaml_fast.safe_load(K3S_DEFAULTS.read_text())[
        "k3s_cron_fired_stamp_dir"
    ]
    tasks = yaml_fast.safe_load((K3S_ROLE / "tasks" / "health-crons.yml").read_text())
    b2_job = next(
        t["ansible.builtin.cron"]["job"]
        for t in tasks
        if t.get("ansible.builtin.cron", {}).get("cron_file")
        == "b2-deletion-accounting"
    )
    shim = rendered_shell_text("setup", "k3s", "longhorn-backup-health.sh.j2")
    assert b2_job.startswith(
        "touch {{ k3s_cron_fired_stamp_dir }}/b2-deletion-accounting;"
    ), b2_job
    assert (
        f'LONGHORN_B2_DELETIONS_CRON_FIRED_STAMP="{stamp_dir}/b2-deletion-accounting"'
        in shim
    )


def test_cron_liveness_does_not_double_report_a_journal_read_that_failed():
    """Check 9 already reports the unreadable journal; it is one fault, not two."""
    assert _liveness(None) == []


def test_cron_liveness_does_not_run_on_a_window_shorter_than_one_cron_period():
    """Below one period, silence is the normal reading — there is nothing to conclude."""
    assert _liveness([], window=12) == []


def test_cron_liveness_reads_b2_deletions_chatter_as_silence():
    """A run that logged a traceback and died must not read as alive.

    The cron pipes both streams through `logger`, so a traceback IS journal lines under the
    tag. Check 9 stays quiet on it too — a traceback carries no `UNPRICED` — so this arm is
    the only reader that can claim the run never finished.
    """
    traceback_lines = [
        "Traceback (most recent call last):",
        '  File "scripts/diagnostics/probe.py", line 1, in <module>',
        "ConnectionError: loki refused the connection",
    ]
    problems = _liveness(traceback_lines)
    assert len(problems) == 1
    assert problems[0][0] == 4
    assert "b2-deletions has logged nothing in the last 26h" in problems[0][1]


def test_cron_liveness_accepts_a_declined_b2_deletions_run():
    """A disarmed BackupTarget is a legitimate no-op, not a stopped cron."""
    assert _liveness(["b2-deletions: declined: the B2 BackupTarget has no URL"]) == []


def test_cron_liveness_accepts_the_summary_line_the_probe_writes():
    """The coupling test: the literal `probe.py b2-deletions` prints is what this matches.

    The shape lives in two trees with no shared import — the probe emits it, this check
    matches it — so nothing but a test holds the two together.
    """
    from diagnostics.probe_lib import b2_ledger

    assert _liveness([b2_ledger.deletions_summary_line(3, 1, 0)]) == []
    assert _liveness([b2_ledger.deletions_declined_line("disarmed")]) == []


def test_cron_liveness_reads_a_body_line_without_a_summary_as_silence():
    """The priced-total row and the nothing-deleted row are body rows, not a verdict.

    `b2_ledger.py` writes both, but a run that dies after printing one of them never reaches
    `deletions_summary_line`. Accepting them would keep the weaker "any line at all" reading
    alive for two line shapes.
    """
    body_rows = [
        "no new B2 backup deletions in the last 26h",
        "charged 3 deletion(s) over 26h: 186 Class C, priced from the listing",
    ]
    assert not logic.deletions_have_spoken(body_rows)
    problems = _liveness(body_rows)
    assert len(problems) == 1
    assert "b2-deletions has logged nothing in the last 26h" in problems[0][1]
