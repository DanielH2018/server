#!/usr/bin/env python3
"""Guards on check 6 of the backup heartbeat: a failed backup JOB must page, and must self-clear.

Every other check in the heartbeat reads Longhorn's own objects. A recurring job that dies before
Longhorn records anything leaves none of them — no Error backup for check 3, and the surviving
volumes keep the fleet-wide freshness of check 2 green. The only signal is a failed Job
object.

Two properties are load-bearing and neither is obvious from reading the check:

FRESHNESS. Kubernetes never garbage-collects a failed Job — there is no TTL, and the history
limits retain it by design. An unbounded check therefore goes red once and stays red forever,
which is the desensitising that check 3's own comment describes for immortal Error objects
re-reported on every 10-minute tick. The age bound is what makes a failure page and
then clear on its own, whether or not anyone deletes the object.

DATING. A Job retries to its backoff limit before it fails, which can take hours
after creation. Dating the failure by `creationTimestamp` puts a created-yesterday /
failed-this-morning run outside the window at the moment it should page, so the condition's
`lastTransitionTime` has to win.

check 6 lives in longhorn_backup_health_logic.check_failed_jobs(); these guards run against that
function directly rather than grepping the shell source, so a future edit that breaks one of the two properties above fails a real assertion
instead of a text match.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_backup_job_failure_check.py
"""

import sys

from _shell_render import rendered_shell_text
from lib.repo_paths import K3S_FILES

sys.path.insert(0, str(K3S_FILES))
import longhorn_backup_health_logic as logic

HOUR = 3600.0
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _job(name, failed_at=None, created="2027-01-01T00:00:00Z"):
    status = {}
    if failed_at:
        status["conditions"] = [
            {"type": "Failed", "status": "True", "lastTransitionTime": failed_at}
        ]
    return {"metadata": {"name": name, "creationTimestamp": created}, "status": status}


def test_failed_jobs_are_checked_at_all() -> None:
    """A Job with no Failed condition must not page — only batch Jobs feed this check."""
    assert (
        logic.check_failed_jobs([_job("daily-backup-1")], NOW - 24 * HOUR, 24) is None
    )


def test_a_failed_condition_with_status_false_does_not_page() -> None:
    job = _job("daily-backup-1")
    job["status"]["conditions"] = [
        {
            "type": "Failed",
            "status": "False",
            "lastTransitionTime": "2027-01-15T07:00:00Z",
        }
    ]
    assert logic.check_failed_jobs([job], NOW - 24 * HOUR, 24) is None


def test_failed_jobs_raise_a_problem_at_backup_failure_severity() -> None:
    """It has to actually page — rank 2, the tier reserved for 'a backup actively failed'."""
    problem = logic.check_failed_jobs(
        [_job("daily-backup-29775090", failed_at="2027-01-15T07:00:00Z")],
        NOW - 24 * HOUR,
        24,
    )
    assert problem is not None
    assert problem[0] == 2
    assert problem[1].startswith("backup job(s) that failed")


def test_failed_job_check_is_age_bounded() -> None:
    """Without this an old failed Job holds the tile red forever. See the module docstring."""
    old_failure = "2026-08-12T11:28:00Z"
    assert (
        logic.check_failed_jobs(
            [_job("daily-backup-29775090", failed_at=old_failure)], NOW - 24 * HOUR, 24
        )
        is None
    ), "an old failure must self-clear once it ages past the cutoff"


def test_failed_job_is_dated_by_the_condition_not_creation() -> None:
    """An 8h retry window sits between the two on the run this check was written for."""
    # Created outside a 6h cutoff, but the Failed condition transitioned inside it.
    problem = logic.check_failed_jobs(
        [
            _job(
                "daily-backup-29775090",
                created="2027-01-15T00:00:00Z",
                failed_at="2027-01-15T07:00:00Z",
            )
        ],
        NOW - 6 * HOUR,
        24,
    )
    assert problem is not None, (
        "dating by creationTimestamp misses a job created before the window and failed inside it"
    )


def test_failed_job_check_names_the_job() -> None:
    """`backup job(s) failed` with no name sends triage looking for which tier died."""
    problem = logic.check_failed_jobs(
        [_job("daily-backup-29775090", failed_at="2027-01-15T07:00:00Z")],
        NOW - 24 * HOUR,
        24,
    )
    assert problem is not None
    assert "daily-backup-29775090" in problem[1]


def test_the_shim_still_calls_the_ported_reader() -> None:
    """The shell no longer holds check 6's logic; it must still invoke the module that does.

    Read off the RENDER rather than the template: the shim's path to the reader is built from
    role defaults, so a source-text search would match `{{ ... }}` and pass over nothing (#3186).
    """
    text = rendered_shell_text("setup", "k3s", "longhorn-backup-health.sh.j2")
    assert "longhorn_backup_health.py" in text
