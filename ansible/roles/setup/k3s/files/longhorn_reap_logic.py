"""Pure decision core shared by the two Longhorn reap-orphan entry points.

Ported from templates/longhorn-reap-orphan-backups.sh.j2 and
templates/longhorn-reap-orphan-snapshots.sh.j2, which carried this same logic as bash string
processing over `kubectl -o jsonpath` output, with no test. FLOOR 1 of the backups reaper
was inoperative from the day it shipped (2026-08-16): ownership was read with
`-o jsonpath='{range .metadata.labels}...'`, and kubectl jsonpath does not iterate a MAP-valued
field — `{range}` over one emits the whole label object as a single space-free token, so the
prefix match never fired and the ownership map came back empty for every volume. That failed
OPEN, not closed: the `$JOB == ${OWNER[$VOL]:-}` comparison then treated any backup with no
RecurringJob label (a hand-triggered probe) as proof the volume's current tier was producing
backups, which stands FLOOR 1 down. On wg-easy-config that would have deleted 3 of its 5
backups while its tier had produced none. The dry run printed `0 reapable`, which reads as
"nothing to do" and was the bug's own output.

Both entry points now read Longhorn objects with `kubectl get ... -o json` and hand the parsed
`.items` lists to the classifiers below, which are plain functions over dicts/lists/sets — no
subprocess, no kubectl, no jsonpath. That is what makes FLOOR 1 (and every other floor here)
provable with a fixture instead of read off a dry-run line.

Stdlib only. Imported by longhorn_reap_orphan_backups.py and longhorn_reap_orphan_snapshots.py,
which do the kubectl reads/writes and printing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

RECURRING_JOB_GROUP_PREFIX = "recurring-job-group.longhorn.io/"


# ── shared ────────────────────────────────────────────────────────────────────────────────


def abort_reason(volume_count: int, owner_count: int) -> str | None:
    """None when the ownership lookup is usable; an ABORT message otherwise.

    Every volume in this cluster carries exactly one recurring-job group label, so an empty
    ownership map while volumes exist means the lookup broke -- and a broken lookup is
    indistinguishable from "nothing is stranded" in a classifier's output. Refuse rather than
    silently let every floor below run disarmed, which is what the jsonpath bug did.
    """
    if volume_count > 0 and owner_count == 0:
        return (
            "ABORT: %d volume(s) exist but none has a resolvable recurring-job group.\n"
            "The ownership lookup is broken; every floor below depends on it. Refusing to "
            "continue." % volume_count
        )
    return None


def resolve_kubeconfig(
    *,
    needs_admin: bool,
    admin_readable: bool,
    admin_path: str,
    readonly_path: str,
    sudo_hint: str,
) -> tuple[str | None, str | None]:
    """Choose which kubeconfig a run should use, or refuse. Returns (path, error).

    A delete-capable run (--apply / --apply-deleted-volumes) needs the admin kubeconfig: the
    read-only ServiceAccount used for the dry-run path returns Forbidden on every delete, and
    without a return-code check that reads as a clean run that deleted nothing -- the shape
    documented in the module docstring's sibling incident. `error` set means refuse to run;
    `path` is None only when `error` is set or when the caller supplies no readonly_path.
    """
    if needs_admin:
        if not admin_readable:
            return None, (
                "deleting needs the admin kubeconfig at %s, which is root-only.\n"
                "Re-run as: %s" % (admin_path, sudo_hint)
            )
        return admin_path, None
    return (readonly_path or None), None


def parse_rfc3339_epoch(stamp: str) -> float | None:
    """Seconds since the epoch for a Kubernetes RFC 3339 UTC timestamp, or None if unparseable."""
    try:
        return (
            datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError, TypeError:
        return None


def _newest_first(records: list[dict], vol_key: str, created_key: str) -> list[dict]:
    """Sort by volume ascending, then creation time descending within each volume.

    Two stable sorts rather than one composite key: `sort -t'|' -k2,2 -k3,3r` needs volume
    ascending and time descending at once, and Python's `sorted` is stable, so sorting by time
    descending first and then by volume preserves the time ordering inside each volume's group.
    """
    by_time = sorted(records, key=lambda r: r[created_key], reverse=True)
    return sorted(by_time, key=lambda r: r[vol_key])


# ── backups reaper ───────────────────────────────────────────────────────────────────────


def backup_owner_map(volumes: list[dict]) -> dict[str, str]:
    """Volume name -> the recurring-job GROUP its label names.

    For the backups reaper the group name IS what a Backup's own RecurringJob label carries
    (daily-backup, weekly-backup-d3, ...), so no RecurringJob-CR indirection is needed here —
    unlike the snapshots reaper below, whose labels are truncated. Read from `.metadata.labels`
    as a parsed dict, never from a jsonpath `{range}` over that map — see the module docstring.
    """
    owner: dict[str, str] = {}
    for v in volumes:
        name = (v.get("metadata") or {}).get("name")
        if not name:
            continue
        for key in (v.get("metadata") or {}).get("labels") or {}:
            if key.startswith(RECURRING_JOB_GROUP_PREFIX):
                owner[name] = key[len(RECURRING_JOB_GROUP_PREFIX) :]
    return owner


def existing_volume_set(volumes: list[dict]) -> set[str]:
    return {
        v["metadata"]["name"] for v in volumes if (v.get("metadata") or {}).get("name")
    }


@dataclass
class BackupClassification:
    # (name, volume, reason)
    kept: list[tuple[str, str, str]] = field(default_factory=list)
    # (name, volume, created, job)
    candidates: list[tuple[str, str, str, str]] = field(default_factory=list)
    orphaned: list[tuple[str, str, str, str]] = field(default_factory=list)


def _backup_fields(b: dict) -> tuple[str, str, str, str, str]:
    name = (b.get("metadata") or {}).get("name", "")
    status = b.get("status") or {}
    vol = status.get("volumeName", "")
    created = status.get("snapshotCreatedAt", "")
    job = (status.get("labels") or {}).get("RecurringJob", "")
    state = status.get("state", "")
    return name, vol, created, job, state


def classify_backups(
    backups: list[dict], owner: dict[str, str], existing_volumes: set[str]
) -> BackupClassification:
    """Sort backups into kept-by-a-floor, reapable, and orphaned (volume deleted).

    A `Completed` backup with no RecurringJob label is a hand-triggered probe: it is neither
    current nor stranded, so BOTH the counting pass and the classification pass skip it
    outright. Skipping it in the counting pass is FLOOR 1's precondition -- counting it would
    let one probe backup stand in as proof the current tier is producing backups, which is
    exactly how FLOOR 1 was disarmed on wg-easy-config (see the module docstring).
    """
    completed = [b for b in backups if _backup_fields(b)[4] == "Completed"]
    labelled = [b for b in completed if _backup_fields(b)[3]]

    current_tier_count: dict[str, int] = {}
    for b in labelled:
        _name, vol, _created, job, _state = _backup_fields(b)
        if job == owner.get(vol, ""):
            current_tier_count[vol] = current_tier_count.get(vol, 0) + 1

    result = BackupClassification()
    seen_floor: set[str] = set()
    records = [
        {"name": n, "vol": v, "created": c, "job": j, "state": s}
        for n, v, c, j, s in (_backup_fields(b) for b in labelled)
    ]
    for b in _newest_first(records, "vol", "created"):
        name, vol, created, job = b["name"], b["vol"], b["created"], b["job"]

        if vol not in existing_volumes:
            result.orphaned.append((name, vol, created, job))
            continue

        if job == owner.get(vol, ""):
            continue  # produced by the job that still owns this volume: current, not stranded

        if current_tier_count.get(vol, 0) == 0:
            result.kept.append((name, vol, "current tier has produced no backup"))
            continue

        if vol not in seen_floor:
            seen_floor.add(vol)
            result.kept.append((name, vol, "newest stray, kept as a floor"))
            continue

        result.candidates.append((name, vol, created, job))

    return result


# ── snapshots reaper ─────────────────────────────────────────────────────────────────────


def recurringjob_group_to_job(recurringjobs: list[dict]) -> dict[str, str]:
    """RecurringJob CR group -> job name, e.g. `weekly-backup-d3` -> `weekly-backup`."""
    mapping: dict[str, str] = {}
    for rj in recurringjobs:
        job = (rj.get("metadata") or {}).get("name")
        if not job:
            continue
        for group in (rj.get("spec") or {}).get("groups") or []:
            mapping[group] = job
    return mapping


def snapshot_owner_map(
    volumes: list[dict], group_job: dict[str, str]
) -> dict[str, str]:
    """Volume name -> the job that owns it now, via its group label and the RecurringJob CR.

    The indirection is load-bearing. A volume's label records its GROUP (`weekly-backup-d3`);
    a snapshot records the JOB that made it (`weekly-backup`, truncated by Longhorn). Those
    strings differ for every weekly shard, so comparing label-suffix to job name directly would
    report every weekly-tier volume's own current snapshots as stranded.
    """
    owner: dict[str, str] = {}
    for v in volumes:
        name = (v.get("metadata") or {}).get("name")
        if not name:
            continue
        for key in (v.get("metadata") or {}).get("labels") or {}:
            if key.startswith(RECURRING_JOB_GROUP_PREFIX):
                group = key[len(RECURRING_JOB_GROUP_PREFIX) :]
                owner[name] = group_job.get(group, "")
    return owner


def attached_volume_set(volumes: list[dict]) -> set[str]:
    return {
        (v.get("metadata") or {}).get("name", "")
        for v in volumes
        if (v.get("status") or {}).get("state") == "attached"
    }


@dataclass
class SnapshotClassification:
    kept: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (name, volume, reason)
    candidates: list[tuple[str, str, str, str]] = field(
        default_factory=list
    )  # (name, vol, created, job)


def _snapshot_fields(s: dict) -> tuple[str, str, str, str, bool]:
    name = (s.get("metadata") or {}).get("name", "")
    vol = (s.get("spec") or {}).get("volume", "")
    status = s.get("status") or {}
    created = status.get("creationTime", "")
    job = (status.get("labels") or {}).get("RecurringJob", "")
    # An unpopulated markRemoved (a snapshot read moments after creation) is absent from the
    # dict, not `false` -- treat it as not-removed the same as an explicit `false`. R14.
    removed = bool(status.get("markRemoved", False))
    return name, vol, created, job, removed


def classify_snapshots(
    snapshots: list[dict],
    owner: dict[str, str],
    attached: set[str],
    min_age_days: float,
    now_epoch: float,
) -> SnapshotClassification:
    """Sort snapshots into kept-by-a-floor and reapable.

    Floor order matches the shell original: newest-per-volume (never a candidate, no reason
    recorded — it isn't bucketed at all), already-removed (skipped silently, coalescing is the
    engine's job), detached (FLOOR 0, reported), unlabelled/hand-taken (skipped silently),
    current-tier (PREFIX match: the owning job name must start with the truncated label the
    snapshot carries), then the age floor (FLOOR 2).
    """
    cutoff = now_epoch - min_age_days * 86400
    raw_records = [
        {"name": n, "vol": v, "created": c, "job": j, "removed": r}
        for n, v, c, j, r in (_snapshot_fields(s) for s in snapshots)
    ]
    records = _newest_first(raw_records, "vol", "created")

    result = SnapshotClassification()
    newest_seen: set[str] = set()
    for r in records:
        name, vol, created, job, removed = (
            r["name"],
            r["vol"],
            r["created"],
            r["job"],
            r["removed"],
        )

        if vol not in newest_seen:
            newest_seen.add(vol)
            continue  # FLOOR 1: the volume's current local restore point, whoever made it

        if removed:
            continue  # already marked removed; the engine coalesces it on the next purge

        if vol not in attached:
            result.kept.append((name, vol, "volume detached — no engine to purge it"))
            continue

        if not job:
            continue  # hand-triggered or a system snapshot Longhorn made on attach

        owner_job = owner.get(vol, "")
        if owner_job and owner_job.startswith(job):
            continue  # made by the job that still owns this volume: current, not stranded

        created_epoch = parse_rfc3339_epoch(created)
        if created_epoch is None:
            result.kept.append((name, vol, "unparseable creationTime: %s" % created))
            continue
        if created_epoch > cutoff:
            result.kept.append((name, vol, "younger than %sd" % min_age_days))
            continue

        result.candidates.append((name, vol, created, job))

    return result
