#!/usr/bin/env python3
"""What the backup-health reader reads off the cluster's BackupTargets and RecurringJobs.

The reader (longhorn_backup_health.py) used to take each target's arm state and each tier's run
time from env vars the shim baked from role defaults. From H1.7b the `longhorn-backup` tag
owns the targets and jobs, and Flux applies a flipped arm flag or schedule with no host apply
behind it, so a baked value would go stale. The reader now lists both kinds and reports what
the cluster holds, whichever tool wrote it (H1 plan, H1.7a). It passes the `.items` here. Everything below is pure: it takes parsed JSON and
returns data, so the decisions are unit-testable without a cluster, like the checks in
longhorn_backup_health_logic.py.

Must stay importable on the host Python floor, with no import beyond the standard library and
the sibling `longhorn_lib` package (see longhorn_backups.py).
"""

from __future__ import annotations

from longhorn_lib import longhorn_backups as backups


def target_state(item: dict) -> dict:
    """One BackupTarget item as the availability check reads it: {"armed", "raw", "reason"}.

    A target is armed while `spec.backupTargetURL` holds anything but whitespace. `raw` is
    `status.available` as the string `check_target_availability` compares ("true" or "false", or
    "unreported" when the status has no `available`), and `reason` is the first condition's
    message, which that check shows only for a target that is not available.
    """
    spec, status = item.get("spec") or {}, item.get("status") or {}
    available = status.get("available")
    raw = "unreported" if available is None else str(bool(available)).lower()
    conditions = status.get("conditions") or []
    message = (conditions[0] or {}).get("message", "") if conditions else ""
    return {
        "armed": bool(str(spec.get("backupTargetURL") or "").strip()),
        "raw": raw,
        "reason": str(message)[:160],
    }


def targets_by_name(items: list[dict]) -> dict[str, dict]:
    """Every BackupTarget's `target_state`, keyed by `metadata.name`."""
    return {
        (item.get("metadata") or {}).get("name", ""): target_state(item)
        for item in items
    }


def watch_plan(
    targets: dict[str, dict] | None,
) -> tuple[list[str], list[str], list[tuple[int, str]]]:
    """(watched targets, disarmed targets, problems) for the two targets the reader knows.

    A blank URL is the one deliberate disarm. A target the list did not return, and every target
    when the list failed (`targets` is None), stays WATCHED: an unreadable cluster must never
    suppress a target's volumes from the checks. A target missing from a list that did arrive is
    a rank-1 problem, so the list failing and the list lacking a target both read DOWN.
    """
    watched: list[str] = []
    disarmed: list[str] = []
    problems: list[tuple[int, str]] = []
    for name in (backups.B2_TARGET, backups.R2_TARGET):
        state = (targets or {}).get(name)
        if state is not None and not state["armed"]:
            disarmed.append(name)
            continue
        watched.append(name)
        if targets is not None and state is None:
            problems.append(
                (
                    1,
                    f"backup target {name} is not in the cluster (a missing target is "
                    "never read as a disarm)",
                )
            )
    return watched, disarmed, problems


def job_crons_by_name(items: list[dict]) -> dict[str, str]:
    """Every RecurringJob's `spec.cron`, keyed by `metadata.name`; an absent cron is empty."""
    return {
        (item.get("metadata") or {}).get("name", ""): str(
            (item.get("spec") or {}).get("cron") or ""
        )
        for item in items
    }


def hhmm_from_two_field_cron(spec: str) -> str | None:
    """`MINUTE HOUR ...` -> `H:MM`, matching the shell's `awk '{ print $2 ":" $1 }'`.

    Works for a full 5-field cron ("30 3 * * *"). Returns None on fewer than two fields rather
    than raising: awk's own `$2`/`$1` on a short line silently prints an empty field, no exit, so
    the shell only ever lost the first-run grace projection on a malformed value — never the
    whole heartbeat. An IndexError here once fired before any of the other checks ran, taking
    the whole reader down over one bad schedule string. logic.first_run_after() treats None the
    same way it treats any other unparseable hhmm: the grace is disabled for that tier.
    """
    fields = spec.split()
    if len(fields) < 2:
        return None
    minute, hour = fields[0], fields[1]
    return f"{hour}:{minute}"


def tier_jobs() -> list[str]:
    """The RecurringJobs with a tier of their own: the daily job, then the weekly shards."""
    return [
        backups.DAILY_JOB,
        *(backups.weekly_shard_group(shard) for shard in range(backups.WEEKLY_SHARDS)),
    ]


def run_times(job_crons: dict[str, str]) -> dict[str, str | None]:
    """Each tier job's `H:MM` run time, or None where its cron is missing or malformed.

    The first-run grace reads these. A None disables that tier's grace and leaves the reader
    running, because a missing job is itself caught by the freshness and coverage checks.
    """
    return {
        job: hhmm_from_two_field_cron(job_crons.get(job, "")) for job in tier_jobs()
    }


def legacy_run_time(times: dict[str, str | None]) -> str | None:
    """The retired flat weekly group's run time. It has no job, so it takes the first shard's."""
    return next(
        (
            times[backups.weekly_shard_group(shard)]
            for shard in range(backups.WEEKLY_SHARDS)
            if times[backups.weekly_shard_group(shard)]
        ),
        None,
    )
