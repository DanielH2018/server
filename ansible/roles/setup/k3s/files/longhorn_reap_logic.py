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


class ReapAbort(RuntimeError):
    """A classifier refused to run because its ownership inputs are unusable.

    Carries the `abort_reason` message. Raised rather than returned because the two
    classifiers return a bucketed result and every empty bucket reads as "nothing to do" --
    the exact shape the module docstring's incident had. An entry point should catch this,
    print it and exit non-zero.
    """


# ── shared ────────────────────────────────────────────────────────────────────────────────


def abort_reason(
    volume_count: int = 0,
    owner_count: int | None = None,
    *,
    recurringjob_count: int | None = None,
    volumes_with_group_label: int | None = None,
    backup_count: int = 0,
    unresolved_owner_count: int = 0,
) -> str | None:
    """None when the ownership lookup is usable; an ABORT message otherwise.

    Four independent refusals, each keyed only on the counts its own caller supplies. Every
    count a caller cannot honestly produce is left out, and its rule stays inert: `owner_count`
    and `recurringjob_count` default to None rather than 0 so an omitted count never reads as
    "counted zero", and the two int counts default to 0 so an omitted one never trips a `> 0`.

    1. Volumes exist but none resolved an owner (`volume_count`, `owner_count`). Every volume
       in this cluster carries exactly one recurring-job group label, so an empty ownership map
       while volumes exist means the lookup broke -- and a broken lookup is indistinguishable
       from "nothing is stranded" in a classifier's output. Refuse rather than silently let
       every floor below run disarmed, which is what the jsonpath bug did.
    2. No volumes at all, but backups exist (`volume_count`, `backup_count`). A `kubectl get
       volumes` that succeeds with zero items -- a wrong namespace, a context with no Longhorn,
       an RBAC that lists nothing rather than erroring -- makes every labelled backup fail the
       `vol not in existing_volumes` test in `classify_backups`, so the whole B2 backup set
       lands in `.orphaned` and `--apply-deleted-volumes` deletes it with no floor. Zero
       volumes AND zero backups is the only honestly empty cluster, and stays clean.
    3. Volumes carry a group label but no RecurringJob CRs exist (`recurringjob_count`,
       `volumes_with_group_label`). `snapshot_owner_map` records an entry for every volume that
       carries a group label REGARDLESS of whether that group resolved to a real RecurringJob,
       so an empty RecurringJob list (owner_count == volume count, every value "") passes rule
       1 silently while every current-tier snapshot reads as stranded.
    4. Some volume resolved to an empty owner (`unresolved_owner_count`). Rule 3 sees only the
       all-or-nothing case: ONE missing or renamed RecurringJob CR leaves a nonzero job count
       and a `""` owner for every volume in that group, and `classify_snapshots`' current-tier
       test (`owner_job and owner_job.startswith(job)`) is False against `""`, so those
       volumes' current snapshots past the age floor all become candidates. Same disarmed
       ownership as rule 3, at per-volume granularity.
    """
    if volume_count > 0 and owner_count == 0:
        return (
            "ABORT: %d volume(s) exist but none has a resolvable recurring-job group.\n"
            "The ownership lookup is broken; every floor below depends on it. Refusing to "
            "continue." % volume_count
        )
    if volume_count == 0 and backup_count > 0:
        return (
            "ABORT: %d backup(s) exist but the volume list is empty. Every one of them would "
            "classify as orphaned and be deleted under --apply-deleted-volumes. An empty "
            "volume list is a broken read, not an empty cluster. Refusing to continue."
            % backup_count
        )
    if recurringjob_count == 0 and (volumes_with_group_label or 0) > 0:
        return (
            "ABORT: %d volume(s) carry a recurring-job group label but no RecurringJob CRs "
            "were found. The group-to-job map is empty, so every current-tier snapshot would "
            "misread as stranded. Refusing to continue." % volumes_with_group_label
        )
    if unresolved_owner_count > 0:
        return (
            "ABORT: %d volume(s) carry a recurring-job group label that resolves to no "
            "RecurringJob CR. A missing or renamed job leaves those volumes with no owner, so "
            "every current-tier snapshot on them would misread as stranded. Refusing to "
            "continue." % unresolved_owner_count
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
    """Seconds since the epoch for a Kubernetes timestamp, or None if unparseable.

    Bash used `date -u -d "$CREATED" +%s`, which accepts fractional seconds and numeric
    offsets, not just a bare trailing `Z` at whole-second precision. `datetime.fromisoformat`
    (3.11+) covers the same ground once a trailing `Z` is mapped to `+00:00` -- fromisoformat
    itself only started accepting `Z` directly in 3.11, and mapping it explicitly here doesn't
    depend on that.

    Bash's fallback on a failed parse was `|| echo 0`, so CREATED_EPOCH==0 was its sentinel for
    "unparseable" -- indistinguishable, in bash, from a real 1970-01-01 timestamp, which no
    Longhorn object legitimately carries. Matched here: an epoch that parses to exactly 0 is
    treated as unparseable too, not as "very old".
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = dt.timestamp()
    return None if epoch == 0 else epoch


def _newest_first(
    records: list[dict], vol_key: str, created_key: str, name_key: str = "name"
) -> list[dict]:
    """Sort by volume ascending, then creation time descending, then name ascending.

    Three stable sorts, least-significant first: `sort -t'|' -k2,2 -k3,3r` only pins volume
    ascending and time descending explicitly; GNU sort breaks a remaining tie (same volume,
    same creation time) by falling back to whole-line comparison, and since both fields already
    agree, that reduces to the NAME field ascending -- the first thing the line differs on.
    Sorting by name first, then time descending, then volume last (each stable) reproduces
    that priority order without a composite key.
    """
    by_name = sorted(records, key=lambda r: r[name_key])
    by_time = sorted(by_name, key=lambda r: r[created_key], reverse=True)
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

    A backup with no NAME or no `.status.volumeName` is skipped before anything else, matching
    bash's `[[ -z "$NAME" || -z "$VOL" ]] && continue` -- without it a completed backup with an
    empty volumeName lands in `.orphaned` (empty string is never in `existing_volumes`) and gets
    deleted under --apply-deleted-volumes for an association that was never real.

    Raises:
      ReapAbort: `existing_volumes` is empty while labelled backups exist -- `abort_reason`'s
        rule 2, checked here rather than in the entry point because the entry point reads the
        backup list only after its own `abort_reason` call.
    """
    valid = [f for f in (_backup_fields(b) for b in backups) if f[0] and f[1]]
    completed = [f for f in valid if f[4] == "Completed"]
    labelled = [f for f in completed if f[3]]

    # `labelled` is the set at risk: the orphan loop below iterates it and nothing else. Only
    # rule 2 is asked -- the entry point already checked ownership against the real volume list,
    # and `owner` here legitimately omits any volume carrying no group label.
    abort = abort_reason(volume_count=len(existing_volumes), backup_count=len(labelled))
    if abort:
        raise ReapAbort(abort)

    current_tier_count: dict[str, int] = {}
    for _name, vol, _created, job, _state in labelled:
        if job == owner.get(vol, ""):
            current_tier_count[vol] = current_tier_count.get(vol, 0) + 1

    result = BackupClassification()
    seen_floor: set[str] = set()
    records = [
        {"name": n, "vol": v, "created": c, "job": j, "state": s}
        for n, v, c, j, s in labelled
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

    A group naming no RecurringJob CR gets the value `""`, which is NOT "this volume has no
    owner" -- a volume with no group label is absent from this map entirely. `""` means the
    lookup for a volume that does carry a label failed, and `unresolved_owner_count` below
    counts exactly those so `classify_snapshots` can refuse.
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


def unresolved_owner_count(owner: dict[str, str]) -> int:
    """How many volumes in a `snapshot_owner_map` resolved to no job.

    Feeds `abort_reason`'s rule 4. Derived from the map rather than counted while building it,
    so it stays a property of the value the classifier actually reads.
    """
    return sum(1 for job in owner.values() if not job)


def volumes_with_group_label(volumes: list[dict]) -> int:
    """How many volumes carry a recurring-job group label at all.

    Feeds `abort_reason`'s RecurringJob-list check (rule 3): `snapshot_owner_map` records an
    entry for every one of these volumes even when `group_job` is empty (the value is just
    `""`), so `owner_count` alone can't tell "the RecurringJob list came back empty" from
    "every group resolved fine".
    """
    count = 0
    for v in volumes:
        labels = (v.get("metadata") or {}).get("labels") or {}
        if any(key.startswith(RECURRING_JOB_GROUP_PREFIX) for key in labels):
            count += 1
    return count


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
    min_age_days: int,
    now_epoch: float,
) -> SnapshotClassification:
    """Sort snapshots into kept-by-a-floor and reapable.

    Floor order: already-removed (skipped silently, coalescing is the engine's job) is checked
    BEFORE newest-per-volume claims FLOOR 1 — an already-removed snapshot must not consume the
    floor slot, or the real newest live snapshot loses its protection and becomes reapable.
    Then newest-per-volume (never a candidate, no reason recorded — it isn't bucketed at all),
    detached (FLOOR 0, reported), unlabelled/hand-taken (skipped silently), current-tier
    (PREFIX match: the owning job name must start with the truncated label the snapshot
    carries), then the age floor (FLOOR 2).

    `min_age_days` is an int, matching `k3s_longhorn_snapshot_reap_min_age_days`'s type in
    defaults/main.yml -- bash's arithmetic context (`$(( NOW - MIN_AGE_DAYS * 86400 ))`) only
    ever held an integer, and "younger than 3d" (not "3.0d") is the message it printed.

    A snapshot with no NAME or no `.spec.volume` is skipped before anything else, matching
    bash's `[[ -z "$NAME" || -z "$VOL" ]] && continue` -- without it an empty volume reaches
    `.candidates` and a later purge POSTs to `/v1/volumes/` (empty path segment) for it.

    Raises:
      ReapAbort: some volume in `owner` resolved to `""` -- `abort_reason`'s rule 4. Only that
        rule is asked: this function is handed no volume list, so any volume or RecurringJob
        count it supplied would be invented.
    """
    abort = abort_reason(unresolved_owner_count=unresolved_owner_count(owner))
    if abort:
        raise ReapAbort(abort)

    cutoff = now_epoch - min_age_days * 86400
    raw_records = [
        {"name": n, "vol": v, "created": c, "job": j, "removed": r}
        for n, v, c, j, r in (_snapshot_fields(s) for s in snapshots)
        if n and v
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

        if removed:
            continue  # already marked removed; the engine coalesces it on the next purge

        if vol not in newest_seen:
            newest_seen.add(vol)
            continue  # FLOOR 1: the volume's current local restore point, whoever made it

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
            result.kept.append((name, vol, "younger than %dd" % min_age_days))
            continue

        result.candidates.append((name, vol, created, job))

    return result
