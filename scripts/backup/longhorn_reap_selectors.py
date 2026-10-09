"""The two operator-selected Backup CR sets: a migrated volume's old chain, and retired seeds.

Companion to longhorn_reap_logic.py, which holds the strays classifier and the shared pieces
both reapers use. Split out because one module cannot hold both and stay under the 600-line cap
`ansible/tests/repo/test_module_length_ratchet.py` enforces; the selection still has exactly one
implementation, and it is this one.

Both selectors answer a different question from `classify_backups`, over a population that
function deliberately never sees. `classify_backups` works on Completed backups that CARRY a
RecurringJob label, because its subject is a tier move: a backup some job made, which no job
will prune now. These two read the Completed set WHOLE, label or no label, because their
subjects are the two ways a Backup CR ends up with no owning job at all:

  migrated-chain  the chain a volume left behind when the block-size migration rebuilt it. Its
                  volume no longer exists, so no job selects it and `retain` -- which is per
                  job, over that job's own backups -- can never reach it.
  seeds           a backup `ansible/seed_volume_backup.yml` made. It carries no RecurringJob
                  label by construction, so no job's `retain` ever counts it.

A stranded chain's members may be labelled or unlabelled (a seed on a deleted volume is the
migration's own debris), so `select_migrated_chain` must not be written as a filter over
`BackupClassification.orphaned`: that list is the LABELLED half, and deriving from it would
narrow the set the retired Jinja selected while reading as a refactor. ENFORCED:
scripts/backup/tests/test_longhorn_reap_selectors.py
::test_migrated_chain_takes_the_unlabelled_strand_classify_backups_cannot_see

THREE DIFFERENT FLOORS, not one. The strays reaper's FLOOR 1 asks whether THIS volume's current
tier has produced a backup. migrated-chain asks whether a DIFFERENT volume -- the replacement
the claim now points at -- holds a Completed backup. seeds asks whether the seed's own volume
holds `floor` Completed backups that DO carry a job label. What the three share is parsed-JSON
selection, the entry point's deletion cap, and its kubeconfig and delete transport.

Stdlib only. Imported by longhorn_reap_orphan_backups.py, which does the kubectl reads/writes
and the printing.
"""

import json
from dataclasses import dataclass, field

import longhorn_reap_logic as logic


SEED_NAME_PREFIX = "seed-"
# The shard jobs' `retain`, which is the state a volume's rotation reaches on its own. A seed
# below it is still that volume's coverage.
SEED_FLOOR_DEFAULT = 2


def backup_claim(backup: dict) -> str:
    """The PVC name Longhorn recorded on a Backup CR, or "" when it recorded none.

    Longhorn writes the PVC into `status.labels.KubernetesStatus` as a JSON STRING, not as a
    struct. The Jinja this replaced matched the quoted key/value pair inside that string with a
    `search` filter, so selecting `n8n-data` depended on the quotes to stop it also selecting
    `n8n-data-something`. Parsing the label and comparing the field makes that structural: there
    is no substring to get wrong. A label that is absent, not JSON, or not an object reads as
    "no claim recorded" and selects nothing.
    """
    raw = ((backup.get("status") or {}).get("labels") or {}).get("KubernetesStatus", "")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    name = doc.get("pvcName", "")
    return name if isinstance(name, str) else ""


def completed_backup_records(backups: list[dict]) -> list[dict]:
    """Every Completed Backup CR as a record, labelled or not.

    A backup with no name or no `.status.volumeName` is dropped for the same reason
    `classify_backups` drops it: an empty volume name is never in `existing_volumes`, so it
    would read as stranded on an association that was never real. Restricting to Completed
    matters too — Longhorn's `retain` never prunes an Error backup, so those are the strays
    reaper's own path, not a mode's.
    """
    records = []
    for item in backups:
        backup = logic.longhorn_backups.from_item(item)
        if not backup.name or not backup.volume or not backup.is_completed:
            continue
        records.append(
            {
                "name": backup.name,
                "vol": backup.volume,
                "created": backup.created,
                "job": backup.job,
                "claim": backup_claim(item),
            }
        )
    return records


def claim_volume(pvcs: list[dict], claim: str, namespace: str) -> tuple[str, str]:
    """The volume backing `claim` right now, or an error naming what was wrong.

    Reads the PVC COLLECTION for one namespace rather than `get pvc <name>`: a collection comes
    back as the same `{"items": [...]}` body every other read here parses, and a claim that does
    not exist is then an empty selection rather than a kubectl exit code to interpret.

    An unbound PVC (`spec.volumeName` empty) is refused by name. Left to fall through it would
    be compared against the live-volume set as `""`, which is never a member, so the refusal
    would come back as "the volume list does not contain the claim's volume" — the message for a
    broken read, pointing the operator at the wrong thing.
    """
    for pvc in pvcs:
        meta = pvc.get("metadata") or {}
        if meta.get("name") != claim:
            continue
        volume = (pvc.get("spec") or {}).get("volumeName") or ""
        if not volume:
            return "", (
                "ABORT: PVC %s/%s is bound to no volume (spec.volumeName is empty), so there "
                "is no replacement chain to check the old one against."
                % (namespace, claim)
            )
        return volume, ""
    return "", (
        "ABORT: no PVC named %s in namespace %s. Pass the claim's own name — the mode selects "
        "by the PVC Longhorn recorded on each backup." % (claim, namespace)
    )


@dataclass
class ChainSelection:
    """What `select_migrated_chain` found. `refusal` set means delete nothing."""

    # (name, volume, created, job)
    stranded: list[tuple[str, str, str, str]] = field(default_factory=list)
    current: list[tuple[str, str, str, str]] = field(default_factory=list)
    refusal: str | None = None


def select_migrated_chain(
    backups: list[dict],
    existing_volumes: set[str],
    *,
    claim: str,
    current_volume: str,
) -> ChainSelection:
    """The claim's stranded chain, and whether its replacement is backed up yet.

    Two refusals, both of which hold in a dry run as well as under --apply, so the dry run says
    whether an apply would be allowed:

    1. `current_volume` is not in `existing_volumes`. The stranded test is "the volume is not in
       this list", so an empty or mis-parsed list makes EVERY backup stranded and deletes a live
       chain. The claim's own volume being in it proves the list was read and parsed, in one
       check.
    2. The replacement holds no Completed backup. The old chain belongs to a volume that no
       longer exists and is, until the replacement has been backed up, the only copy of that
       data. Deleting it then leaves the service with no recovery point at all.
    """
    records = completed_backup_records(backups)
    result = ChainSelection(
        stranded=[
            (r["name"], r["vol"], r["created"], r["job"])
            for r in logic.newest_first(
                [
                    r
                    for r in records
                    if r["claim"] == claim and r["vol"] not in existing_volumes
                ],
                "vol",
                "created",
            )
        ],
        current=[
            (r["name"], r["vol"], r["created"], r["job"])
            for r in records
            if r["vol"] == current_volume
        ],
    )
    if current_volume not in existing_volumes:
        result.refusal = (
            "ABORT: the live-volume list does not contain %s's own volume (%s). Every backup "
            "would be treated as stranded. Nothing has been deleted."
            % (claim, current_volume)
        )
    elif not result.current:
        result.refusal = (
            "REFUSING: %s (now %s) has no Completed backup. Deleting the stranded chain would "
            "leave it with no recovery point at all. Trigger its weekday shard first and wait "
            "for the backup to complete." % (claim, current_volume)
        )
    return result


@dataclass
class SeedSelection:
    """What `select_seeds` found. `refusal` set means delete nothing."""

    # (name, volume, created, job)
    superseded: list[tuple[str, str, str, str]] = field(default_factory=list)
    # (name, volume, reason)
    kept: list[tuple[str, str, str]] = field(default_factory=list)
    refusal: str | None = None


def select_seeds(
    backups: list[dict],
    existing_volumes: set[str],
    *,
    claim: str = "",
    floor: int = SEED_FLOOR_DEFAULT,
) -> SeedSelection:
    """The seeds whose volume's own rotation has superseded them.

    A seed is identified by BOTH markers: the `seed-` name prefix and the absent RecurringJob
    label. The name alone also matches a hand-made object; the missing label alone also matches
    a hand-triggered probe backup, of which the live store holds one.

    THE FLOOR. A seed goes only once its volume holds at least `floor` Completed backups that DO
    carry a RecurringJob label — the state `retain` keeps on its own, at which point the seed is
    surplus. One shard run is not coverage, so a volume below the floor keeps its seed and the
    reason is reported rather than silent.

    An empty `existing_volumes` is refused: it reads as "no volume is live", which would make
    every seed look like a migration orphan rather than a superseded seed, and this mode must
    not decide that. `claim` narrows the selection to one PVC, which is how the Class C spend is
    paced when the whole set does not fit in a day.

    A `claim` no backup carries is refused too, rather than narrowing to nothing. A mistyped PVC
    name — or a volume name passed where a claim name belongs — would otherwise print "0
    superseded seed(s)" and exit 0, which reads as "this volume's seeds are already gone". That
    is the shape of this module's founding incident, where a disarmed floor reported `0 reapable`
    and the operator read it as nothing to do. The migrated-chain mode gets the same refusal from
    `claim_volume`; this one has no PVC read to get it from.
    """
    records = completed_backup_records(backups)
    result = SeedSelection()
    if not existing_volumes:
        result.refusal = (
            "ABORT: the live-volume list is empty. An empty list is a broken read, not an empty "
            "cluster, and every seed would misread as a migration orphan. Nothing has been "
            "deleted."
        )
        return result
    if claim and not any(r["claim"] == claim for r in records):
        result.refusal = (
            "ABORT: no Completed backup records the PVC %s, so narrowing to it selects nothing "
            "and an empty plan would read as 'already retired'. Check the claim name — the mode "
            "matches the PVC Longhorn recorded on each backup, not a volume name."
            % claim
        )
        return result

    rotation_count: dict[str, int] = {}
    for r in records:
        if r["job"]:
            rotation_count[r["vol"]] = rotation_count.get(r["vol"], 0) + 1

    seeds = [
        r
        for r in records
        if r["name"].startswith(SEED_NAME_PREFIX)
        and not r["job"]
        and (not claim or r["claim"] == claim)
    ]
    for r in logic.newest_first(seeds, "vol", "created"):
        name, vol = r["name"], r["vol"]
        if vol not in existing_volumes:
            result.kept.append(
                (name, vol, "volume no longer exists — the migrated-chain mode owns it")
            )
            continue
        held = rotation_count.get(vol, 0)
        if held < floor:
            result.kept.append(
                (
                    name,
                    vol,
                    "rotation holds %d labelled backup(s), below the floor of %d"
                    % (held, floor),
                )
            )
            continue
        result.superseded.append((name, vol, r["created"], r["job"]))
    return result
