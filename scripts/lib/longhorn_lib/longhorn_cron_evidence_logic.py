"""Checks 9 and 10 of the Longhorn backup-plane heartbeat: the trim's and the B2 cron's evidence.

Split out of longhorn_backup_health_logic.py when that module reached its length cap. Same
contract as the module it came from: stdlib only, every clock reading and every read injected,
no cluster needed to test it.

Check 9 reads what each job SAID (#2418). Check 10 reads whether it ran at all (#2443). They are
separate arms because silence and a bad verdict are different faults with different fixes.

- The **filesystem trim** is Longhorn's `trim` RecurringJob (H2.2c of
  docs/target-architecture/host/h2-shrink-host-plane.md). Its arm reads the CronJob Longhorn
  creates for it, that CronJob's newest Job, and the Job's pod log. The pod log is the evidence,
  because longhorn-manager logs a per-volume failure and can still finish the Job as succeeded.
- The **B2 deletion accounting** is a host cron. Its arm reads the `b2-deletions` journal tag,
  the cron file and the cron's fire stamp.

Rank 4 throughout, where `_fetch_text` uses rank 1 for a failed kubectl read. That asymmetry is
deliberate: a failed backup-plane read means the backups are unreadable, while everything here
is the evidence plane. A trim that reclaimed nothing and a deletion nobody could price are both
real and neither is urgent, so they must not displace a stale backup from the push slot.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Resolves via longhorn_backup_health.py's own sys.path.insert, exactly as the sibling logic
# module's does: host_lib.py sits beside this package, not inside it.
import sys as _sys
from pathlib import Path as _Path

# In the checkout, `hosts/lib` holds host_lib.py and `scripts/lib` holds this package. On a
# host both sit beside the reader, whose directory is already on sys.path, so these inserts
# name directories that do not exist there and change nothing.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3] / "hosts" / "lib"))

import host_lib
from host_lib import rfc3339_to_epoch

# ── the trim: Longhorn's `trim` RecurringJob ────────────────────────────────────────────────

# The RecurringJob's name. Longhorn names the CronJob after it and labels the CronJob, its Jobs
# and their pods `recurring-job.longhorn.io=<name>`, and names the pod's container after it.
TRIM_JOB = "trim"
TRIM_JOB_LABEL = "recurring-job.longhorn.io"
# Read in the Longhorn namespace. `--ignore-not-found` makes an absent CronJob rc 0 with empty
# output, so every non-zero exit is a read that failed.
TRIM_CRONJOB_GET_ARGS = (
    "get",
    "cronjobs.batch",
    TRIM_JOB,
    "--ignore-not-found",
    "-o",
    "json",
)

# The per-volume failure lines in longhorn-manager v1.13.0's `app/recurringjob/volume.go`:
# `startJob`'s warning, `startVolumeJob`'s two errors, and the error `doRecurringFilesystemTrim`
# wraps. Matched as written. A healthy run's first line is `Failed to create Longhorn manager API
# client, retrying`, so a looser match on "Failed" would read every run as a failure.
_TRIM_FAILURE_RE = re.compile(
    r"Failed to run volume job|Failed to run recurring job for volume"
    r"|Failed to initialize job for volume|failed to complete filesystem-trim"
)
# Where each failure line names its volume: the volume job's logger field `volumeName=`, the
# `volume=` field `startJob` adds, or the message text `... for volume <vol>` and `... for <vol>`.
_TRIM_VOLUME_RE = re.compile(
    r"\bvolume(?:Name)?=\"?([\w.-]+)|for volume ([\w.-]+)|filesystem-trim for ([\w.-]+)"
)
# `[pod/<pod>/<container>] ...`, the prefix `kubectl logs --prefix` writes.
_LOG_PREFIX_RE = re.compile(r"^\[pod/(?P<pod>[^/\]]+)/(?P<container>[^\]]+)\] ")

# The slack a run gets past one period of the CronJob's schedule before check 10 calls it
# stopped. The same 2h the B2 arm's 26h window gives its daily cron.
_TRIM_SLACK_S = 2 * 3600


def schedule_period_s(schedule: str) -> int | None:
    """Seconds between two runs of a `MINUTE HOUR * * *` or `MINUTE HOUR * * DOW` schedule.

    Returns None for any other shape, such as a list or a step in a field: check 10 then says it
    cannot judge the cadence rather than guess one.
    """
    fields = schedule.split()
    if len(fields) != 5 or not all(f.isdigit() for f in fields[:2]):
        return None
    if fields[2:] == ["*", "*", "*"]:
        return 86400
    if fields[2:4] == ["*", "*"] and fields[4].isdigit():
        return 7 * 86400
    return None


def _created(item: dict) -> float | None:
    return rfc3339_to_epoch(
        str((item.get("metadata") or {}).get("creationTimestamp") or "")
    )


def newest_trim_job(job_items: list[dict]) -> dict | None:
    """The newest Job the trim CronJob created, by creationTimestamp, or None when there is none."""
    jobs = [
        item
        for item in job_items
        if ((item.get("metadata") or {}).get("labels") or {}).get(TRIM_JOB_LABEL)
        == TRIM_JOB
    ]
    if not jobs:
        return None
    return max(jobs, key=lambda item: _created(item) or 0.0)


def _job_failed(item: dict) -> bool:
    return any(
        c.get("type") == "Failed" and c.get("status") == "True"
        for c in ((item.get("status") or {}).get("conditions") or [])
    )


def trim_failures(pod_log: str, job_name: str | None) -> list[str]:
    """The volumes, or failure lines where no volume is named, the trim's last run failed on.

    Reads only the `trim` container, and only the pods of `job_name` when one is given: a failed
    Job's pod can outlive it beside a newer, clean run.
    """
    failures: dict[str, None] = {}
    for line in pod_log.splitlines():
        prefix = _LOG_PREFIX_RE.match(line)
        if not prefix or prefix["container"] != TRIM_JOB:
            continue
        if job_name and not prefix["pod"].startswith(job_name + "-"):
            continue
        if not _TRIM_FAILURE_RE.search(line):
            continue
        volume = _TRIM_VOLUME_RE.search(line)
        key = next(filter(None, volume.groups())) if volume else line[:120]
        failures[key.strip('"')] = None
    return list(failures)


def _trim_liveness(
    cronjob: dict, newest: dict | None, now_s: float
) -> tuple[int, str] | None:
    """Check 10's trim arm: whether the CronJob still starts a Job as often as its schedule says."""
    spec = cronjob.get("spec") or {}
    schedule = str(spec.get("schedule") or "")
    if spec.get("suspend"):
        return (4, f"the {TRIM_JOB} CronJob is suspended, so no filesystem trim runs")
    period_s = schedule_period_s(schedule)
    if period_s is None:
        return (
            4,
            f"the {TRIM_JOB} CronJob's schedule {schedule!r} is not a daily or weekly cron, "
            "so whether it still runs cannot be judged",
        )
    allowed_s = period_s + _TRIM_SLACK_S
    status = cronjob.get("status") or {}
    last_s = rfc3339_to_epoch(str(status.get("lastScheduleTime") or ""))
    if last_s is None and newest is not None:
        last_s = _created(newest)
    if last_s is None:
        created_s = _created(cronjob)
        if created_s is not None and now_s - created_s < allowed_s:
            return None
        return (4, f"the {TRIM_JOB} CronJob ({schedule}) has never started a Job")
    if now_s - last_s > allowed_s:
        return (
            4,
            f"the {TRIM_JOB} CronJob ({schedule}) last started a Job "
            f"{int((now_s - last_s) / 3600)}h ago — it has stopped running",
        )
    return None


def check_trim(
    cronjob: dict | None,
    job_items: list[dict] | None,
    pod_log: str | None,
    now_s: float,
) -> list[tuple[int, str]]:
    """Checks 9 and 10 for Longhorn's `trim` RecurringJob.

    Args:
        cronjob: the CronJob `TRIM_CRONJOB_GET_ARGS` returned, or None when it does not exist.
        job_items: `.items` of every Job in the Longhorn namespace, or None when that read failed.
        pod_log: `kubectl logs -l recurring-job.longhorn.io --prefix` output, or None when that
            read failed.
        now_s: the epoch every age is measured from.

    A failed read of the Jobs or the pod log skips the arm that needs it, because the caller has
    already reported that read as a problem of its own.
    """
    if cronjob is None:
        return [
            (
                4,
                f"the {TRIM_JOB} RecurringJob has no CronJob, so Longhorn schedules no "
                "filesystem trim",
            )
        ]
    if job_items is None:
        return []
    problems: list[tuple[int, str]] = []
    newest = newest_trim_job(job_items)
    liveness = _trim_liveness(cronjob, newest, now_s)
    if liveness:
        problems.append(liveness)
    name = ((newest or {}).get("metadata") or {}).get("name")
    if newest is not None and _job_failed(newest):
        problems.append(
            (
                4,
                f"the {TRIM_JOB} Job {name} failed — see its pod log in longhorn-system",
            )
        )
    if pod_log is not None:
        failed = trim_failures(pod_log, name)
        if failed:
            problems.append(
                (
                    4,
                    f"the {TRIM_JOB} job failed on {len(failed)} volume(s) on its last run — "
                    "freed blocks are still being backed up: " + ", ".join(failed[:5]),
                )
            )
    return problems


# ── the B2 deletion accounting: a host cron ─────────────────────────────────────────────────


def check_deletion_evidence(
    deletion_lines: list[str] | None, window_hours: int
) -> list[tuple[int, str]]:
    """Check 9's B2 arm: whether the deletion accounting reported an UNPRICED deletion.

    The cron writes its verdict through `logger`, and nothing read it until this check (#2418).
    `deletion_lines` are the `b2-deletions` journal lines over the last `window_hours`, or None
    when the read itself failed.
    """
    if deletion_lines is None:
        return [
            (
                4,
                "could not read the b2-deletions journal — an UNPRICED deletion has no reader",
            )
        ]
    # The whole window is searched, not just the newest run, for two reasons. `logger` writes
    # one journal entry per line and probe.py's UNPRICED report is a multi-line block, so
    # "the newest run" cannot be reconstructed from the lines alone. And an unpriced deletion
    # is a permanent EVENT rather than a current state — the transactions it spent cannot be
    # recovered by any later run — so the right behaviour is to report it once and let it age
    # out of the window, which it does on its own.
    unpriced = [line for line in deletion_lines if "UNPRICED" in line]
    if unpriced:
        return [
            (
                4,
                f"b2-deletions reported UNPRICED in the last {window_hours}h "
                f"(Class C spent that no later run can price): {unpriced[0].strip()[:160]}",
            )
        ]
    return []


# One period of the cron. `health-crons.yml` schedules it with an hour and a minute and NO
# `weekday`, so it fires once a day. If it gains a weekday, this constant and the window guard
# in `check_deletion_liveness` change together — a weekly cron is legitimately silent for six
# days and the guard would have to compare against its own period instead.
_CRON_PERIOD_S = 86400

# `b2-deletions: charged 3, skipped 0, unpriced 0` and `b2-deletions: declined: ...` —
# `deletions_summary_line` and `deletions_declined_line` in
# `scripts/diagnostics/probe_lib/b2_ledger.py`, matched as written. A completed run prints
# exactly one of them.
_DELETIONS_SUMMARY_RE = re.compile(
    r"b2-deletions: charged \d+, skipped \d+, unpriced \d+"
)
_DELETIONS_DECLINED_RE = re.compile(r"b2-deletions: declined: ")


class CronState(NamedTuple):
    """What the host knows about a host cron, apart from what it logged.

    Attributes:
        tag: the syslog tag the cron's verdict lines carry. Named in the message because it is
            how an operator finds the cron again (`journalctl -t <tag>`).
        path: the `/etc/cron.d` file Ansible installs the cron as.
        installed_at: that file's mtime, or None when the file does not exist.
        unreadable: the file exists but could not be stat'd — a permissions change, most
            likely. Distinct from a missing file because "the cron is gone" and "I could not
            look" have different fixes, and folding them sends whoever is paged to the wrong one.
        expected: whether this host installs the cron at all. The B2-accounting cron is gated
            on `has_repo_checkout` and a host without a checkout never gets one.
        fired_stamp: the file the cron line itself touches before it runs its job.
            `/etc/cron.d` is 0700 root on these hosts (initial_setup's CIS hardening), so the
            sys_user this check runs as can never stat `path`. The stamp is the reader that
            needs no root: it says the cron FIRED, which is the half of "installed and
            firing" that matters (#3677).
        fired_at: the stamp's mtime, or None when it does not exist or could not be read.
    """

    tag: str
    path: str
    installed_at: float | None
    unreadable: bool
    expected: bool
    fired_stamp: str = ""
    fired_at: float | None = None


def deletions_have_spoken(lines: list[str]) -> bool:
    """Whether the window holds a line a COMPLETED `b2-deletions` run writes.

    `probe.py b2-deletions` ends every run that completes with one of two fixed shapes —
    `deletions_summary_line` for a run that classified, `deletions_declined_line` for a
    disarmed backup target (`scripts/diagnostics/probe_lib/b2_ledger.py`). Judging on those
    rather than on any line at all is #2545: the cron pipes both streams through `logger`, so a
    run that logged a traceback and exited non-zero used to read as alive here, and check 9
    stayed quiet too because a traceback carries no `UNPRICED`.
    """
    return any(
        _DELETIONS_SUMMARY_RE.search(line) or _DELETIONS_DECLINED_RE.search(line)
        for line in lines
    )


def check_deletion_liveness(
    lines: list[str] | None,
    cron: CronState,
    window_hours: int,
    now_s: float,
) -> tuple[int, str] | None:
    """Check 10's B2 arm: whether the cron still fires, as distinct from what it said.

    Check 9 reads the cron's CONTENT and stays green on an empty window, so a cron whose entry
    is removed or whose crontab is lost would leave check 9 green forever (#2443). Silence alone
    is not the alarm. Three things legitimately produce it, and each is answered without a new
    tunable:

    - A window shorter than one cron period. Then silence is the normal reading on most ticks,
      so the arm does not run at all.
    - A cron this host does not install (`CronState.expected`).
    - A cron installed less than one window ago: a freshly provisioned host, or one Ansible has
      just rewritten. The window is already one period plus slack, so it is the grace too.

    The arm judges silence on a RECOGNISED line, so it proves the cron COMPLETED rather than
    merely fired (#2545).
    """
    if window_hours * 3600 < _CRON_PERIOD_S:
        return None
    if not cron.expected or (lines and deletions_have_spoken(lines)):
        return None
    if lines is None:
        # Check 9 already reports the failed journal read, and it is the same fault. A second
        # problem off one unreadable journal would double-count it into the push slot.
        return None
    window_s = window_hours * 3600
    if cron.fired_at is not None and now_s - cron.fired_at < window_s:
        # Cron ran it and it wrote no line a COMPLETED run writes: the job failed or was cut
        # short. Its entry is plainly still installed, so neither message below applies.
        fired_h = int((now_s - cron.fired_at) / 3600)
        return (
            4,
            f"{cron.tag} fired {fired_h}h ago but logged no completed run in the last "
            f"{window_hours}h — see journalctl -t {cron.tag}",
        )
    if cron.unreadable:
        stamp = (
            f"its fire stamp {cron.fired_stamp} is older than that or missing"
            if cron.fired_stamp
            else "it has no fire stamp"
        )
        return (
            4,
            f"{cron.tag} has not fired in the last {window_hours}h ({stamp}); could not "
            f"stat {cron.path}, so whether its entry is still installed needs root to check",
        )
    if cron.installed_at is None:
        return (
            4,
            f"the {cron.tag} cron is not installed ({cron.path}) and nothing ran it in "
            f"the last {window_hours}h",
        )
    if now_s - cron.installed_at < window_s:
        # A freshly provisioned host, or one whose cron Ansible has just rewritten. It has not
        # had a full window to fire in, so its silence proves nothing. The window is the grace
        # because the window is already one period plus slack — no second tunable.
        return None
    return (
        4,
        f"{cron.tag} has logged nothing in the last {window_hours}h — its cron has "
        f"stopped firing ({cron.path})",
    )


def cron_state(tag: str, path: str, expected: bool, fired_stamp: str) -> CronState:
    """`CronState` for the cron installed at `path`, reading its install and fire times.

    An unreadable stamp reads as no stamp: the fallback below it is the `/etc/cron.d` stat,
    which names its own blindness.
    """
    installed_at, unreadable = host_lib.file_mtime(path)
    fired_at, _ = host_lib.file_mtime(fired_stamp)
    return CronState(
        tag, path, installed_at, unreadable, expected, fired_stamp, fired_at
    )


def check_deletions(
    journal,
    window_hours: int,
    deletion: CronState,
    now_s: float,
) -> list[tuple[int, str]]:
    """Checks 9 and 10 for the B2 deletion accounting, over ONE journal read.

    `journal(tag)` is `host_lib.journal_reader`'s bound reader: the lines that tag logged in
    the window, or None when the read itself failed. The two arms share the read because they
    are two questions about the same lines — what the cron said, and whether it said anything.
    """
    lines = journal(deletion.tag)
    problems = check_deletion_evidence(lines, window_hours)
    liveness = check_deletion_liveness(lines, deletion, window_hours, now_s)
    if liveness:
        problems.append(liveness)
    return problems
