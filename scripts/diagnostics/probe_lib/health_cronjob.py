"""The CronJob half of `probe.py health` — a role with a CronJob and no rollout to gate on.

Split out of probe_lib/health.py, which had grown to 938 lines. `format_cronjob_health` is the
analog of `health_rollout.format_k8s_health` for the two CronJob-only roles (configarr,
pi-peer-backup): a CronJob has no rollout, so the gate reads its most recent owned Job instead.

WHAT "NOT FOUND" IS ALLOWED TO MEAN governs the absence messages here too — health.py's module
docstring is the canonical statement, and `format_role_cronjob_health` resolves these names
from rendered manifests, so "no CronJob in this namespace" must NOT read as a skip.
"""

import re
from dataclasses import dataclass

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.health_rollout import seconds_since

# RBAC decides which of two paths this gate can take: trigger a fresh run itself, the way the
# retired `k8s/cronjob-gate` role did at deploy time, or fall back to reading the most recent existing run.
# probe.py runs as `homelab-readonly` (see `roles/k8s/cluster-base/templates/readonly-rbac.yaml.j2`),
# bound to the built-in `view` ClusterRole plus one additive ClusterRole, neither granting any
# write verb. Verified live, running as that identity:
#   k3s kubectl auth can-i create jobs -n homelab   -> no
#   k3s kubectl auth can-i get jobs -n homelab      -> yes
#   k3s kubectl auth can-i list jobs -n homelab     -> yes
#   k3s kubectl auth can-i get cronjobs -n homelab  -> yes
#   k3s kubectl auth can-i list cronjobs -n homelab -> yes
# So the READ-ONLY FALLBACK IS THE ONLY PATH LIVE HERE: this module never creates a Job, only
# reads the CronJob and its existing Jobs. That is the whole reason `format_cronjob_health`
# has a schedule-fallback branch at all — the trigger-a-run half is not
# available to this identity, ever, by design (the readonly SA is a read path, deliberately).
# The gate run itself is a Job Flux applies in `<tag>-verify`.


# A Flux-owned role's gate Job (#5953). Its `<role>-verify` Kustomization applies it, so it has
# no `ownerReferences`: `scripts/renderer/renderer_lib/cronjob_gate.py:gate_name` names it
# `<cronjob>-deploy-gate-<spec digest8>`, and Flux labels it with the Kustomization's name.
# The exact name pins the CronJob, so a CronJob whose name extends this one's never matches.
# The label pins the applier: an ownerless Job a person made with that name proves nothing.
FLUX_OWNER_LABEL = "kustomize.toolkit.fluxcd.io/name"
_GATE_INFIX = "-deploy-gate-"
_DIGEST8 = re.compile(r"^[0-9a-f]{8}$")


# Which gate Job proves the revision Flux applies (#5955). A Flux-owned role has no release
# record, so no `applied_at` to compare a Job's creation time against. The renderer stamps the
# same digest twice instead: `scripts/renderer/renderer_lib/bundle.py:CONTENT_DIGEST` on
# Kustomization `<tag>` carries the tag's `own` digest, and
# `scripts/renderer/renderer_lib/cronjob_gate.py:ROLE_DIGEST8` on the gate Job's pod template
# carries its first eight characters. Verified live 2026-10-23 for configarr,
# longhorn-reconcile and pi-peer-backup.
CONTENT_DIGEST = "homelab/content-digest"
ROLE_DIGEST8 = "homelab/role-digest8"


@dataclass(frozen=True)
class FluxRevision:
    """A Flux-owned role's reference for `format_cronjob_health`, in place of `applied_at`.

    Attributes:
        digest8: The first eight characters of Kustomization `<tag>`'s `CONTENT_DIGEST`, or
            None when the Kustomization is missing or carries no such annotation.
        gate: The name of the gate Job whose pod template carries `digest8`, or None when
            no gate Job does.
        applied_at: When Flux first applied the revision it applies now
            (`revision_applied_at`), or None when the Kustomization does not say. Only a
            schedule-gated CronJob (`SCHEDULE_GATE`) reads it.
    """

    digest8: str | None
    gate: str | None
    applied_at: str | None = None


def _is_owned_by(job, cronjob_name):
    """True when a controller `ownerReferences` entry on `job` names `cronjob_name`."""
    return any(
        ref.get("kind") == "CronJob"
        and ref.get("name") == cronjob_name
        and ref.get("controller")
        for ref in (job.get("metadata") or {}).get("ownerReferences") or []
    )


def _is_flux_gate_job(job, cronjob_name):
    """True when `job` is the gate Job a `-verify` Kustomization applied for `cronjob_name`."""
    meta = job.get("metadata") or {}
    prefix = f"{cronjob_name}{_GATE_INFIX}"
    name = meta.get("name") or ""
    if not name.startswith(prefix) or not _DIGEST8.match(name[len(prefix) :]):
        return False
    applier = (meta.get("labels") or {}).get(FLUX_OWNER_LABEL) or ""
    return applier.endswith("-verify")


def latest_owned_job(jobs_doc, cronjob_name):
    """The most recently created Job run for `cronjob_name`, or None if it has none.

    Three ways a Job can exist for a CronJob, and this covers all three:
      - the schedule firing it;
      - a run triggered by hand from the CronJob's template, which sets the same controller
        `ownerReferences` entry a firing does (verified live 2026-08-21);
      - a Flux-owned role's `<role>-verify` Kustomization applying its gate Job, which carries
        no `ownerReferences` (`_is_flux_gate_job`).
    """
    owned = [
        job
        for job in (jobs_doc or {}).get("items") or []
        if _is_owned_by(job, cronjob_name) or _is_flux_gate_job(job, cronjob_name)
    ]
    if not owned:
        return None
    return max(
        owned,
        key=lambda job: (job.get("metadata") or {}).get("creationTimestamp") or "",
    )


def flux_revision(kustomization, jobs_doc, cronjob_name):
    """The `FluxRevision` of `cronjob_name` for a Flux-owned role.

    Args:
        kustomization: Kustomization `<tag>` from `flux-system`, or None when it is missing.
        jobs_doc: Every Job in the CronJob's namespace, as `latest_owned_job` reads it.
        cronjob_name: The CronJob whose gate Job to find.
    """
    annotations = ((kustomization or {}).get("metadata") or {}).get("annotations") or {}
    digest8 = (annotations.get(CONTENT_DIGEST) or "")[:8] or None
    applied_at = revision_applied_at(kustomization)
    if digest8 is None:
        return FluxRevision(None, None, applied_at)
    for job in (jobs_doc or {}).get("items") or []:
        template = (job.get("spec") or {}).get("template") or {}
        stamped = ((template.get("metadata") or {}).get("annotations") or {}).get(
            ROLE_DIGEST8
        )
        if stamped == digest8 and _is_flux_gate_job(job, cronjob_name):
            return FluxRevision(digest8, job["metadata"]["name"], applied_at)
    return FluxRevision(digest8, None, applied_at)


def revision_applied_at(kustomization):
    """When Flux first applied the revision Kustomization `kustomization` applies now, or None.

    kustomize-controller keeps one `status.history` entry per applied content digest, and the
    entry whose `metadata.revision` is `status.lastAppliedRevision` is the live one. Its
    `firstReconciled` is when that content went live. Verified live 2026-10-25 on
    `longhorn-restore-drill`: the entry before the live one starts at 17:38:28Z on 2026-10-24,
    the second the CronJob's kustomize-controller `managedFields` entry records for the unsuspend.
    """
    status = (kustomization or {}).get("status") or {}
    revision = status.get("lastAppliedRevision")
    if not revision:
        return None
    for entry in status.get("history") or []:
        if (entry.get("metadata") or {}).get("revision") == revision:
            return entry.get("firstReconciled") or None
    return None


def _flux_revision_failure(name, job_name, revision):
    """The failure text when `revision` has no gate Job of its own, else None."""
    if revision.digest8 is None:
        return (
            f"{name}: the role's Flux Kustomization is missing or carries no "
            f"{CONTENT_DIGEST} annotation, so no gate Job can be matched to the revision "
            "Flux applies — failing closed"
        )
    if revision.gate is None:
        return (
            f"{name}: no gate Job carries the revision Flux applies ({ROLE_DIGEST8} "
            f"{revision.digest8}); the newest Job, {job_name}, ran an earlier revision or "
            "its schedule — the `-verify` Kustomization has not run this revision's gate"
        )
    return None


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


# `M H * * *` (daily) and `M H * * D` (weekly) are the schedule shapes this sizes: configarr,
# pi-peer-backup and all seven Longhorn backup jobs. longhorn-reconcile runs hourly
# (`17 * * * *`), so it passes only on a gate Job newer than its deploy. This is
# deliberately not a general cron parser: a schedule outside these two shapes returns None and
# the caller fails closed rather than guess what "normal interval" means for it.
_DAILY_SCHEDULE = re.compile(r"^\s*\d+\s+\d+\s+\*\s+\*\s+\*\s*$")
_WEEKLY_SCHEDULE = re.compile(r"^\s*\d+\s+\d+\s+\*\s+\*\s+[0-6]\s*$")

# How many schedule intervals a CronJob-only role's last known run may age past before the
# schedule-fallback path (below) calls it overdue rather than merely "hasn't fired again yet".
# 2x rather than 1x: a run that fires a few minutes late every so often is normal cluster jitter,
# not a stalled CronJob, and this path is only reached when release_stamp.yml also says the
# latest owned run predates the deploy it should be proving.
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


def declared_suspended(docs, default_namespace):
    """`namespace/name` of each CronJob among `docs` whose rendered spec sets `suspend: true`.

    The intent half of the suspend check: a role's manifest says a CronJob ships suspended on
    purpose (the restore drill did, until H2.3e), and the live object is held to that.
    """
    return frozenset(
        f"{(doc.get('metadata') or {}).get('namespace') or default_namespace}/"
        f"{(doc.get('metadata') or {}).get('name')}"
        for doc in docs
        if doc.get("kind") == "CronJob"
        and (doc.get("spec") or {}).get("suspend") is True
    )


# The marker a CronJob carries when nothing runs it per revision and its schedule is the gate
# (#6128). The restore drill carries it: a gate Job would run a real restore beside the
# scheduled one, under the same `restore-drill-<pvc>` name
# (`ansible/tests/deploy/test_cronjob_only_roles_include_the_gate.py:UNGATED_BY_DESIGN`).
SCHEDULE_GATE = "homelab/gate"
SCHEDULE_GATE_VALUE = "schedule"


def declared_schedule_gated(docs, default_namespace):
    """`namespace/name` of each CronJob among `docs` whose rendered metadata sets `SCHEDULE_GATE`."""
    return frozenset(
        f"{(doc.get('metadata') or {}).get('namespace') or default_namespace}/"
        f"{(doc.get('metadata') or {}).get('name')}"
        for doc in docs
        if doc.get("kind") == "CronJob"
        and ((doc.get("metadata") or {}).get("annotations") or {}).get(SCHEDULE_GATE)
        == SCHEDULE_GATE_VALUE
    )


def format_schedule_gated(name, cronjob, latest_job, pods, revision, now):
    """(text, exit code) for a Flux-owned CronJob whose schedule is its gate.

    No gate Job carries the revision, so `_flux_revision_failure` would fail it for good. The
    revision's apply time stands in for `applied_at` instead, and is never optional here:

      - No Job yet: a pass for one schedule period after the apply, the longest a firing can
        take to arrive, and a failure after it.
      - A Job: `format_cronjob_health`'s release-record path, against the apply time.
    """
    if cronjob is None:
        return format_cronjob_health(name, cronjob, latest_job, pods, None, now)
    if not isinstance(revision, FluxRevision) or revision.applied_at is None:
        return (
            f"{name}: the role's Flux Kustomization records no apply time for the "
            "revision it applies (no `status.history` entry for its "
            "`lastAppliedRevision`), so its schedule cannot be judged — failing closed",
            1,
        )
    if latest_job is not None:
        return format_cronjob_health(
            name, cronjob, latest_job, pods, revision.applied_at, now
        )
    schedule = (cronjob.get("spec") or {}).get("schedule")
    interval = _schedule_interval_seconds(schedule)
    age = seconds_since(revision.applied_at, now)
    if interval is None or age is None:
        return (
            f"{name}: no Job has run yet, and either its schedule ({schedule!r}) or the "
            f"revision's apply time ({revision.applied_at!r}) cannot be read as an interval "
            "— failing closed rather than guess",
            1,
        )
    if age > interval:
        return (
            f"{name}: no Job found for this CronJob, {int(age)}s after the revision applied "
            f"against a {interval}s schedule — its first scheduled run never came",
            1,
        )
    return (
        f"{name}: no Job has run yet; the revision applied {int(age)}s ago, inside one "
        f"{schedule!r} period, so its first scheduled run is still due",
        0,
    )


def format_suspended(name, cronjob, declared):
    """(text, exit code) for a live-suspended CronJob, or None when it is not suspended.

    A suspended CronJob fires no Job, so the gate has no run to read and would fail it as "no
    evidence it has ever run". That is right for a CronJob that should run and wrong for one the
    manifest suspends on purpose, so the two read differently. `declared` is the set
    `declared_suspended` builds from the same render the gate resolves its targets with.

      - Suspended live and in the manifest: a pass, with nothing to gate until it is unsuspended.
      - Suspended live and not in the manifest: a failure. Someone suspended it by hand, which
        is drift and not intent, and the drill or sync it runs has stopped.
    """
    if cronjob is None or (cronjob.get("spec") or {}).get("suspend") is not True:
        return None
    if name in declared:
        return f"{name}: suspended; nothing to gate until it is unsuspended", 0
    return (
        f"{name}: suspended in the cluster, but its manifest does not declare "
        "`suspend: true` — drift, not intent; unsuspend it or declare it",
        1,
    )


def format_cronjob_health(name, cronjob, latest_job, pods, deploy_applied_at, now):
    """(text, exit code) for one CronJob-only workload's post-deploy verification.

    The CronJob analog of format_k8s_health: a Deployment role is gated on `rollout status`
    plus a restart check, and a CronJob has no rollout, so this reads the CronJob's most
    recent Job run for it instead: the gate Job a Flux role's `-verify` Kustomization applies,
    or the next scheduled firing if that hasn't landed.

    For a Flux-owned role `deploy_applied_at` is a `FluxRevision`. With no gate Job carrying
    the revision Flux applies this fails, and otherwise `latest_job` is that gate or newer, so
    it is gated directly as the first path below does.

    For any other role, two paths, chosen by comparing `latest_job`'s creation time against
    `deploy_applied_at` (`release_stamp.yml`'s `applied_at` for this service, or None when
    unreadable):

      - `latest_job` is newer than the deploy (a scheduled firing or a hand-created run since the
        deploy) — gated directly on IT: it must have succeeded, and no container in
        its pod may have restarted.
      - `latest_job` predates the deploy, or `deploy_applied_at` is unreadable — the gate this
        role's own deploy should have run either did not run or has not been read yet. Falls
        back to the CronJob's own schedule: the previous run must have succeeded, and its age
        must be within CRONJOB_STALE_MULTIPLIER schedule intervals — a schedule shape this
        cannot size an interval for (anything but plain daily/weekly) fails closed rather than
        guess.

    This does not distinguish "the image never started" from "the application failed". The
    retired `k8s/cronjob-gate` role was lenient on the latter because it ran on every deploy,
    including ones where the failure is a known-transient dependency; this is a general
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
    job_age = seconds_since(job_created, now)
    if job_age is None:
        return (
            f"{name}: could not read {job_name}'s creation time ({job_created!r}) — "
            "failing closed",
            1,
        )

    if isinstance(deploy_applied_at, FluxRevision):
        failure = _flux_revision_failure(name, job_name, deploy_applied_at)
        if failure:
            return failure, 1
        # `latest_owned_job` takes the newest Job of a set that holds the revision's gate, so
        # `latest_job` is that gate or a firing created after it.
        fresh = True
    else:
        deploy_age = seconds_since(deploy_applied_at, now)
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
