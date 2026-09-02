#!/usr/bin/env python3
"""Guards on the seed-backup playbook — the one that writes a first recovery point to B2.

It exists because `migrate_volume_block_size.yml` returns a volume with no backup history, and
`k3s-bringup.yml --tags longhorn` then puts it back in a weekly shard that runs on one weekday. On
2026-08-20 fourteen volumes were in that state at once, none of them overdue and none of them
holding a recovery point anywhere: the backup-health check's first-run grace excuses a volume until
its first scheduled run, so the exposure is invisible by design rather than by defect.

Each refusal below prevents a specific way of spending a B2 transaction on a backup that is
useless, unprunable, or impossible:

NO TIER. Longhorn's retain is per job. A backup of a volume no job selects is never pruned, which
is how the estate accumulated the orphans the reaper was written for.

DISARMED TARGET. A blank backupTargetURL is how this estate disarms a target. A Backup requested
against one cannot complete, and leaves a stuck CR behind.

ALREADY COVERED. A volume with a backup is on its job's cadence. Seeding it again buys nothing the
schedule does not already bound.

ERROR IS NOT SUCCESS. `kubectl apply` succeeds the moment the CR is accepted; the backup itself can
still fail minutes later. A play that ends after the apply reports a recovery point that does not
exist - the same shape as the readonly-SA rollout restart that prints "successfully rolled out".

Run: uv run pytest ansible/tests/longhorn/test_seed_volume_backup.py
"""

import yaml
from _helpers import ANSIBLE

PLAYBOOK = ANSIBLE / "seed_volume_backup.yml"
TEXT = PLAYBOOK.read_text()
PLAY = yaml.safe_load(TEXT)[0]
TASKS = PLAY["tasks"]


def _named(fragment):
    return [t for t in TASKS if fragment.lower() in (t.get("name") or "").lower()]


def test_it_refuses_a_volume_in_no_backup_tier():
    """Longhorn's retain is per job, so a tierless backup is never pruned."""
    task = _named("in no backup tier")
    assert task, "expected a refusal for a volume in no recurring-job group"
    conditions = str(task[0]["ansible.builtin.assert"]["that"])
    assert "no-backup" in conditions and "seed_group" in conditions


def test_it_refuses_a_disarmed_target():
    """A blank backupTargetURL cannot complete a backup, and leaves a stuck CR behind."""
    task = _named("disarmed or unavailable")
    assert task, "expected a refusal for a disarmed or unavailable backup target"
    conditions = str(task[0]["ansible.builtin.assert"]["that"])
    assert "backupTargetURL" in TEXT
    assert "length > 0" in conditions and "'true'" in conditions


def test_it_refuses_a_volume_that_already_has_a_backup():
    """The point is a FIRST recovery point; a covered volume is on its job's cadence."""
    task = _named("already has a backup")
    assert task, "expected a refusal for an already-covered volume"
    assert "seed_existing" in str(task[0]["ansible.builtin.assert"]["that"])


def test_it_waits_for_completion_and_fails_on_error():
    """kubectl apply succeeds when the CR is accepted, not when the backup exists."""
    wait = _named("wait for the backup to complete")
    assert wait, "expected a wait on the Backup's terminal state"
    until = str(wait[0]["until"])
    assert "Completed" in until and "Error" in until, (
        "the wait must treat Error as terminal too, or an errored backup spins until the retry "
        "budget runs out and reports a timeout rather than the error Longhorn recorded"
    )
    fail = _named("errored")
    assert fail, "expected an explicit failure on a non-Completed terminal state"
    assert "Completed" in str(fail[0]["ansible.builtin.assert"]["that"])


def test_the_backup_carries_both_routing_labels():
    """backup-target routes the request; backup-volume is how the BackupVolume finds its members."""
    assert "backup-target: {{ seed_target }}" in TEXT
    assert "backup-volume: {{ seed_pv.stdout }}" in TEXT


def test_the_target_is_read_from_the_volume_not_assumed():
    """This estate runs two targets: R2 daily, B2 weekly shards. Assuming one silently mis-routes."""
    assert "spec.backupTargetName" in TEXT, (
        "the target must come from the volume's own spec.backupTargetName - the tier -> target "
        "mapping is inventory and can be re-pointed per volume"
    )


def test_every_refusal_runs_before_anything_is_created():
    """A guard after the first write is not a guard."""
    names = [t.get("name") or "" for t in TASKS]
    first_write = min(
        i for i, n in enumerate(names) if "create the snapshot" in n.lower()
    )
    for fragment in (
        "in no backup tier",
        "disarmed or unavailable",
        "already has a backup",
    ):
        idx = next(i for i, n in enumerate(names) if fragment.lower() in n.lower())
        assert idx < first_write, (
            "'%s' must be asserted before the snapshot is created" % fragment
        )


MIGRATE = (ANSIBLE / "migrate_volume_block_size.yml").read_text()


def test_the_migrate_playbook_points_at_the_seed_step():
    """Restoring the tier label is not the same as being covered.

    `k3s-bringup.yml --tags longhorn` puts a rebuilt volume back in a weekly shard, and a weekly
    shard runs on one weekday. The backup-health check's first-run grace then excuses the volume
    until that day arrives, so it can hold no recovery point in either store for up to 6 days while
    every monitor reads green. The migrate playbook has to name the seed step, or the operator
    following its instructions stops one step short of coverage.
    """
    assert "seed_volume_backup.yml" in MIGRATE, (
        "migrate_volume_block_size.yml must tell the operator to seed a first backup after "
        "restoring the tier label - relabelling alone leaves a multi-day uncovered window"
    )
