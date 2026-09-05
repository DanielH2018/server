"""The CronJob half of `probe.py health` — a role with a CronJob and no rollout to gate on.

Split out of probe_lib/health.py, which had grown to 938 lines. `format_cronjob_health` is the
analog of `health_rollout.format_k8s_health` for the two CronJob-only roles (configarr,
pi-peer-backup): a CronJob has no rollout, so the gate reads its most recent owned Job instead.

WHAT "NOT FOUND" IS ALLOWED TO MEAN governs the absence messages here too — health.py's module
docstring is the canonical statement, and `format_role_cronjob_health` resolves these names
from rendered manifests, so "no CronJob in this namespace" must NOT read as a skip.
"""

import re

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.health_rollout import _seconds_since

# RBAC decides which of two paths this gate can take: trigger a fresh run itself, the way
# `k8s/cronjob-gate` does at deploy time, or fall back to reading the most recent existing run.
# probe.py runs as `homelab-readonly` (see `roles/setup/k3s/templates/readonly-rbac.yaml.j2`),
# bound to the built-in `view` ClusterRole plus one additive ClusterRole, neither granting any
# write verb. Verified live 2026-09-03, running as that identity:
#   k3s kubectl auth can-i create jobs -n homelab   -> no
#   k3s kubectl auth can-i get jobs -n homelab      -> yes
#   k3s kubectl auth can-i list jobs -n homelab     -> yes
#   k3s kubectl auth can-i get cronjobs -n homelab  -> yes
#   k3s kubectl auth can-i list cronjobs -n homelab -> yes
# So the READ-ONLY FALLBACK IS THE ONLY PATH LIVE HERE: this module never creates a Job, only
# reads the CronJob and its existing Jobs. That is the whole reason `format_cronjob_health`
# has a schedule-fallback branch at all — the trigger-a-run half `k8s/cronjob-gate` uses is not
# available to this identity, ever, by design (the readonly SA is a read path, deliberately,
# and `k8s/cronjob-gate` runs under Ansible's escalated connection instead).


def latest_owned_job(jobs_doc, cronjob_name):
    """The most recently created Job owned by `cronjob_name`, or None if it has none.

    `kubectl create job --from=cronjob/<name>` sets a controller `ownerReferences` entry
    pointing at the CronJob, the same as a scheduled firing does — verified live, see
    `roles/k8s/cronjob-gate/CLAUDE.md` ("Why a CronJob needs this at all"). So this covers
    both ways a Job can exist for a CronJob: the schedule firing it itself, and
    `k8s/cronjob-gate` triggering an out-of-band run at deploy time.
    """
    owned = [
        job
        for job in (jobs_doc or {}).get("items") or []
        for ref in (job.get("metadata") or {}).get("ownerReferences") or []
        if ref.get("kind") == "CronJob"
        and ref.get("name") == cronjob_name
        and ref.get("controller")
    ]
    if not owned:
        return None
    return max(
        owned,
        key=lambda job: (job.get("metadata") or {}).get("creationTimestamp") or "",
    )


def _job_outcome(job):
    """'succeeded', 'failed' or 'running' for a Job, read from its status.

    `status.conditions` is the documented source (`Complete`/`Failed`, both `status: "True"`
    once set), but `status.succeeded`/`status.failed` are populated first and conditions can
    lag a beat behind them — so both are checked and either is enough.
    """
    status = (job or {}).get("status") or {}
    conditions = {
        c.get("type"): c.get("status") for c in status.get("conditions") or []
    }
    if conditions.get("Complete") == "True" or status.get("succeeded", 0) >= 1:
        return "succeeded"
    if conditions.get("Failed") == "True" or status.get("failed", 0) >= 1:
        return "failed"
    return "running"


# `M H * * *` (daily) and `M H * * D` (weekly) are the only two schedule shapes any CronJob in
# this cluster uses today — `k3s kubectl get cronjob -A`, checked 2026-09-03: configarr,
# pi-peer-backup and all seven Longhorn backup jobs. This is deliberately not a general cron
# parser: a schedule outside these two shapes returns None and the caller fails closed rather
# than guess what "normal interval" means for it.
_DAILY_SCHEDULE = re.compile(r"^\s*\d+\s+\d+\s+\*\s+\*\s+\*\s*$")
_WEEKLY_SCHEDULE = re.compile(r"^\s*\d+\s+\d+\s+\*\s+\*\s+[0-6]\s*$")

# How many schedule intervals a CronJob-only role's last known run may age past before the
# schedule-fallback path (below) calls it overdue rather than merely "hasn't fired again yet".
# 2x rather than 1x: a run that fires a few minutes late every so often is normal cluster jitter,
# not a stalled CronJob, and this path is only reached when release_stamp.yml also says the
# deploy-time k8s/cronjob-gate run either never happened or predates what it should be proving.
CRONJOB_STALE_MULTIPLIER = 2


def _schedule_interval_seconds(schedule):
    """Seconds between firings for a daily or weekly CronJob `schedule`, or None if neither."""
    if not schedule:
        return None
    if _DAILY_SCHEDULE.match(schedule):
        return 86400
    if _WEEKLY_SCHEDULE.match(schedule):
        return 7 * 86400
    return None


def format_cronjob_health(name, cronjob, latest_job, pods, deploy_applied_at, now):
    """(text, exit code) for one CronJob-only workload's post-deploy verification.

    The CronJob analog of format_k8s_health: a Deployment role is gated on `rollout status`
    plus a restart check, and a CronJob has no rollout, so this reads the CronJob's most
    recent owned Job instead — the same Job `k8s/cronjob-gate` creates at deploy time
    (`roles/k8s/cronjob-gate/CLAUDE.md`), or the next scheduled firing if that hasn't landed.

    Two paths, chosen by comparing `latest_job`'s creation time against `deploy_applied_at`
    (`release_stamp.yml`'s `applied_at` for this service, or None when unreadable):

      - `latest_job` is newer than the deploy (the normal case: `k8s/cronjob-gate` runs one at
        every real deploy) — gated directly on IT: it must have succeeded, and no container in
        its pod may have restarted.
      - `latest_job` predates the deploy, or `deploy_applied_at` is unreadable — the gate this
        role's own deploy should have run either did not run or has not been read yet. Falls
        back to the CronJob's own schedule: the previous run must have succeeded, and its age
        must be within CRONJOB_STALE_MULTIPLIER schedule intervals — a schedule shape this
        cannot size an interval for (anything but plain daily/weekly) fails closed rather than
        guess.

    Unlike `k8s/cronjob-gate` itself, this does not distinguish "the image never started" from
    "the application failed" — cronjob-gate is lenient on the latter because it runs on every
    deploy, including ones where the failure is a known-transient dependency; this is a general
    health read with no such context, so it is a plain pass/fail, the same as
    `format_k8s_health`.
    """
    if cronjob is None:
        return (
            f"{name}: no CronJob in this namespace "
            "(wrong name, wrong namespace, or the deploy never ran?)",
            1,
        )
    if latest_job is None:
        return (
            f"{name}: no Job found for this CronJob — no evidence it has ever run",
            1,
        )

    job_name = (latest_job.get("metadata") or {}).get("name", "?")
    job_created = (latest_job.get("metadata") or {}).get("creationTimestamp")
    job_age = _seconds_since(job_created, now)
    if job_age is None:
        return (
            f"{name}: could not read {job_name}'s creation time ({job_created!r}) — "
            "failing closed",
            1,
        )

    deploy_age = _seconds_since(deploy_applied_at, now)
    fresh = deploy_age is None or job_age < deploy_age

    outcome = _job_outcome(latest_job)
    if outcome == "running":
        return (f"{name}: {job_name} has not finished yet — cannot confirm success", 1)
    if outcome == "failed":
        return (
            f"{name}: {job_name} FAILED"
            + ("" if fresh else " (and it predates the last deploy)"),
            1,
        )

    restarts = sum(
        cs.get("restartCount", 0)
        for pod in (pods or {}).get("items") or []
        for cs in (pod.get("status") or {}).get("containerStatuses") or []
    )
    if restarts:
        return (
            f"{name}: {job_name} succeeded but a container in its pod restarted "
            f"(restarts={restarts})",
            1,
        )

    if fresh:
        return (f"{name}: {job_name} succeeded since the last deploy, no restarts", 0)

    schedule = (cronjob.get("spec") or {}).get("schedule")
    interval = _schedule_interval_seconds(schedule)
    if interval is None:
        return (
            f"{name}: no Job has run since the last deploy (the last one, {job_name}, "
            f"predates it), and its schedule ({schedule!r}) is not one this gate can size an "
            "interval for — failing closed rather than guess",
            1,
        )
    if job_age > interval * CRONJOB_STALE_MULTIPLIER:
        return (
            f"{name}: no Job has run since the last deploy, and the previous successful run "
            f"({job_name}) is {int(job_age)}s old against a {interval}s schedule — overdue",
            1,
        )
    return (
        f"{name}: no Job has run since the last deploy yet, but the previous run "
        f"({job_name}) succeeded {int(job_age)}s ago and is within its {schedule!r} schedule",
        0,
    )
