#!/usr/bin/env python3
"""Check 11 of the backup heartbeat: a weekly volume a RecurringJob skipped must page.

The reader-side half, that the pod-log read reaches this check with the right flags, is
`test_longhorn_backup_health_reader.py::test_reader_pages_naming_a_weekly_volume_its_job_skipped`.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_skipped_volumes.py
"""

from longhorn_lib import longhorn_skipped_volumes_logic as logic
from host_lib import rfc3339_to_epoch

# The 2026-10-08 d4 line as `kubectl logs --prefix` printed it, nanosecond timestamp included.
SKIP_LINE = (
    "[pod/weekly-backup-d4-29857230-cnd4h/weekly-backup-d4] "
    'time="2027-01-14T21:55:26.299183893Z" level=warning '
    'msg="Cannot create job for pvc-prowlarr volume in state attached" '
    'func=recurringjob.filterVolumesForJob file="util.go:109"'
)
SKIPPED_AT = rfc3339_to_epoch("2027-01-14T21:55:26Z")
WATCHED = {"pvc-prowlarr": "prowlarr/prowlarr-config"}


def test_skip_line_parses_to_job_volume_and_time():
    other = '[pod/daily-backup-1-abc/daily-backup] time="2027-01-15T03:30:00Z" msg="Starting volume job"'
    assert logic.parse_skipped_volumes(f"{other}\n{SKIP_LINE}\n") == [
        ("weekly-backup-d4", "pvc-prowlarr", SKIPPED_AT)
    ]


def test_skipped_weekly_volume_is_flagged_with_no_backup_since():
    old = [("pvc-prowlarr", "2027-01-08T04:30:00Z", "weekly-backup-d4")]
    problem = logic.check_skipped_weekly_volumes(
        logic.parse_skipped_volumes(SKIP_LINE), WATCHED, old
    )
    assert problem is not None
    assert problem[0] == 3
    assert (
        "prowlarr/prowlarr-config (weekly-backup-d4 at 2027-01-14T21:55Z)" in problem[1]
    )


def test_skipped_weekly_volume_is_cleared_by_a_seed_from_another_job():
    """The doc's remedy is a hand seed, which carries no weekly-backup-d4 label."""
    seeded = [("pvc-prowlarr", "2027-01-14T23:51:00Z", "")]
    assert (
        logic.check_skipped_weekly_volumes(
            logic.parse_skipped_volumes(SKIP_LINE), WATCHED, seeded
        )
        is None
    )


def test_skipped_volume_outside_the_watched_set_is_ignored():
    """A deleted or disarmed volume, or one in the daily tier, is not in `watched`."""
    assert (
        logic.check_skipped_weekly_volumes(
            logic.parse_skipped_volumes(SKIP_LINE),
            {"pvc-other": "default/other"},
            [],
        )
        is None
    )
