#!/usr/bin/env python3
"""Guards on check 6 of the backup heartbeat: a failed backup JOB must page, and must self-clear.

Every other check in longhorn-backup-health.sh reads Longhorn's own objects. A recurring job that
dies before Longhorn records anything leaves none of them — no Error backup for check 3, and the
surviving volumes keep the fleet-wide freshness of check 2 green. `daily-backup-29775090` failed
at 03:30 on 2026-08-12, exhausted its three retries by 11:28, and the only signal anywhere was a
failed Job object nothing read. The B2 transaction cap it died against was found by hand, days
later.

Two properties are load-bearing and neither is obvious from reading the check:

FRESHNESS. Kubernetes never garbage-collects a failed Job — there is no TTL, and the history
limits retain it by design. An unbounded check therefore goes red once and stays red forever,
which is exactly the desensitising that check 3's own comment was written about after 11 immortal
Error objects re-reported on every 10-minute tick. The age bound is what makes a failure page and
then clear on its own, whether or not anyone deletes the object.

DATING. A Job retries to its backoff limit before it fails, and on 2026-08-12 that was eight
hours after creation. Dating the failure by `creationTimestamp` puts a created-yesterday /
failed-this-morning run outside the window at the moment it should page, so the condition's
`lastTransitionTime` has to win.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_backup_job_failure_check.py
"""

import re
from _helpers import ANSIBLE


HEALTH = (
    ANSIBLE / "roles" / "setup" / "k3s" / "templates" / "longhorn-backup-health.sh.j2"
)


def _code() -> str:
    """The script minus its comments — the comments discuss creationTimestamp on purpose."""
    return "\n".join(
        line
        for line in HEALTH.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _check_six() -> str:
    """Just the failed-Job block, so a match cannot be satisfied by an unrelated check."""
    code = _code()
    start = code.index("FAILED_JOBS=")
    end = code.index("fi", code.index('if [[ -n "$FAILED_JOBS" ]]', start))
    return code[start:end]


def test_failed_jobs_are_checked_at_all() -> None:
    """The check exists and reads batch Jobs, not only Longhorn CRs."""
    block = _check_six()
    assert "get jobs.batch" in block, (
        "check 6 must read Jobs; Longhorn's own CRs cannot see this"
    )
    assert '.type == "Failed"' in block
    assert '.status == "True"' in block, (
        "a Failed condition with status False is not a failure"
    )


def test_failed_jobs_raise_a_problem_at_backup_failure_severity() -> None:
    """It has to actually page — rank 2, the tier reserved for 'a backup actively failed'."""
    block = _check_six()
    assert re.search(r'add 2 "backup job\(s\) that failed', block), (
        "a detected failure must call add() at rank 2, or the tile stays green"
    )


def test_failed_job_check_is_age_bounded() -> None:
    """Without this the 2026-08-12 corpse holds the tile red forever. See the module docstring."""
    block = _check_six()
    assert "$cutoff" in block and "ERROR_CUTOFF_S" in block, (
        "check 6 must bound by age; a failed Job is never garbage-collected by Kubernetes"
    )
    assert re.search(r"select\(\s*\.at\s*>\s*\$cutoff\s*\)", block), (
        "the cutoff must filter the results, not merely be passed in"
    )


def test_failed_job_is_dated_by_the_condition_not_creation() -> None:
    """An 8h retry window sits between the two on the run this check was written for."""
    block = _check_six()
    assert "lastTransitionTime" in block, (
        "dating by creationTimestamp misses a job created before the window and failed inside it"
    )
    assert block.index("lastTransitionTime") < block.index("creationTimestamp"), (
        "creationTimestamp may only be the fallback, never the primary"
    )


def test_failed_job_check_names_the_job() -> None:
    """`backup job(s) failed` with no name sends triage looking for which tier died."""
    assert ".name" in _check_six(), "the message must name the failed job"
