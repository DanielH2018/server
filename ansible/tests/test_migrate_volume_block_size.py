#!/usr/bin/env python3
"""Ordering guards on the block-size migration playbook.

This playbook deletes a live PVC and rebuilds it. Everything protecting the data is an ORDER: a
verification that happens before a deletion. Reordering two tasks turns a safe migration into an
irreversible one, and nothing about the YAML makes that visible on review.

Three orders carry the weight:

  VERIFY BEFORE DELETE. The staging copy must be checked before the original PVC is deleted.
  Between the delete and the copy back, staging is the only copy of the data.

  VERIFY BEFORE DISCARD. The copy back must be checked before the staging PVC is removed. A
  mismatch has to leave the verified copy on disk, not clean it up.

  RESTORE IN `always`. The play scales the workload to zero. A failure anywhere in between must
  still bring it back, or a failed migration is also a silent outage.

Run: uv run pytest ansible/tests/test_migrate_volume_block_size.py
"""

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
PLAY = ANSIBLE / "migrate_volume_block_size.yml"
COPY = ANSIBLE / "tasks" / "blockmig_copy.yml"


def _text() -> str:
    return PLAY.read_text()


def _names(tasks) -> list[str]:
    return [t["name"] for t in tasks if isinstance(t, dict) and "name" in t]


def _play() -> dict:
    return yaml.safe_load(_text())[0]


def _main_tasks() -> list:
    return _play()["tasks"]


def _migration_block() -> dict:
    block = next(
        t
        for t in _main_tasks()
        if t.get("name") == "Copy out, verify, rebuild and copy back"
    )
    return block


def test_the_staging_copy_is_verified_before_the_original_is_deleted() -> None:
    """Between this delete and the copy back, staging holds the only copy of the data."""
    names = _names(_migration_block()["block"])
    verify = names.index("Refuse to delete the original when the staging copy is empty")
    delete = names.index("Delete the original claim")
    assert verify < delete, (
        "the original PVC is deleted before the staging copy is checked — an empty or partial "
        "copy would destroy the only remaining data"
    )


def test_the_copy_back_is_verified_before_staging_is_discarded() -> None:
    """A mismatch must leave the verified copy on disk rather than tidying it away."""
    names = _names(_migration_block()["block"])
    verify = names.index("Assert the replacement matches what was copied out")
    discard = names.index("Remove the staging claim")
    assert verify < discard, (
        "the staging volume is removed before the copy back is verified, so a failed migration "
        "would delete the last good copy"
    )


def test_the_workload_is_restored_even_when_the_migration_fails() -> None:
    """The play scales to zero; a failure that skips the scale back up is a silent outage."""
    always = _names(_migration_block()["always"])
    assert "Restore the workload" in always, (
        "the scale back to 1 must be in `always`, or an aborted migration leaves the service down"
    )
    assert "Remove the copy pod" in always, (
        "the copy pod holds both volumes attached and must be removed on every exit path"
    )


def test_the_target_block_size_is_read_from_the_cluster() -> None:
    """Assuming it would rebuild the volume at the OLD size and only fail afterwards.

    The role default and the live setting are different facts. If the setting had never been
    applied, a hardcoded target would pass the "already migrated?" check, destroy the volume,
    rebuild it at 2 MiB and only then notice.
    """
    names = _names(_main_tasks())
    assert "Read the block size new volumes are being created at" in names, (
        "the target block size must come from settings.longhorn.io, not from a role default"
    )
    read = names.index("Read the block size new volumes are being created at")
    quiesce = names.index("Quiesce the workload")
    assert read < quiesce, "read the setting before taking the service down"


def test_the_size_is_checked_against_the_webhook_rule_before_anything_is_touched() -> (
    None
):
    """Longhorn refuses to create a volume whose size is not a multiple of the block size.

    Hitting that after the original is deleted would strand the data on the staging volume with
    no way to rebuild the real one. See test_pvc_sizes_match_block_size.py for the same rule
    enforced against the templates.
    """
    names = _names(_main_tasks())
    check = names.index("Refuse a size the webhook will reject")
    quiesce = names.index("Quiesce the workload")
    assert check < quiesce, (
        "the size must be validated before the workload is taken down, not after the original "
        "PVC has been deleted"
    )


def test_an_already_migrated_volume_is_refused() -> None:
    """Re-running would spend real downtime and strand the backup chain a second time."""
    names = _names(_main_tasks())
    assert (
        "Refuse to migrate a volume that is already at the target block size" in names
    )


def test_the_workload_is_named_explicitly_rather_than_derived() -> None:
    """Scaling the wrong Deployment leaves the real writer running against a volume being copied."""
    asserts = _names(_play()["pre_tasks"])
    assert "Require a claim and its workload" in asserts
    assert "mig_deploy is defined" in _text()


def test_the_copy_verifies_both_count_and_content() -> None:
    """A file count alone passes on truncated files; a digest alone passes on a missing file."""
    copy = COPY.read_text()
    assert "copy_counts" in copy and "copy_digests" in copy
    assert "Assert the copy landed intact" in copy, (
        "the copy task must verify its own result, so a mismatch is reported against the copy "
        "that produced it rather than several tasks later"
    )


def test_the_backup_tier_label_loss_is_called_out() -> None:
    """A rebuilt volume carries no tier label, which puts it in no backup tier at all.

    Nothing pages for this immediately — the backup-health check only notices once the volume
    ages past its tier bound, by which time the operator has moved on.
    """
    text = _text()
    assert "--tags longhorn" in text, (
        "the playbook must tell the operator how to restore the tier label"
    )
    assert "no backup tier" in text


def test_the_quiesce_selector_is_proven_before_anything_is_scaled_down() -> None:
    """A selector matching nothing makes the quiesce wait pass instantly.

    The wait selects pods by `app=<mig_deploy>`. If a Deployment used a different label the wait
    would succeed immediately while the writer kept running, and the copy would capture a live
    filesystem. Nothing downstream would notice — the digests would agree, because both sides are
    read after the same torn copy.
    """
    names = _names(_main_tasks())
    check = names.index("Refuse to migrate when the selector matches no running pod")
    quiesce = names.index("Quiesce the workload")
    assert check < quiesce, (
        "the selector must be proven to match a running pod before the workload is scaled down"
    )


def test_a_volume_with_no_deployment_is_proven_detached_instead() -> None:
    """pi-peer-backup-data is written by a CronJob, so there is nothing to scale.

    Skipping the quiesce without replacing it would copy whatever state the volume happened to
    be in. `detached` read from Longhorn is the same guarantee the quiesce buys — no writer —
    and a stronger one, because it is read rather than inferred from a pod list.
    """
    names = _names(_main_tasks())
    assert (
        "Require the volume to be detached when there is no workload to quiesce"
        in names
    ), "mig_deploy=none must still prove nothing has the volume open"
    text = _text()
    assert 'status.state == "detached"' in text


def test_every_workload_step_is_skipped_when_there_is_no_workload() -> None:
    """A leftover scale against deploy/none fails the play, and in `always` it hides the real error."""
    guarded = {
        "Confirm the workload's pods are selectable before quiescing",
        "Refuse to migrate when the selector matches no running pod",
        "Quiesce the workload",
        "Wait for the workload's pods to go",
        "Restore the workload",
    }
    seen = set()
    for task in _main_tasks() + _migration_block()["always"]:
        if task.get("name") in guarded:
            seen.add(task["name"])
            assert task.get("when") == 'mig_deploy != "none"', (
                f"{task['name']} must be skipped when there is no Deployment to act on"
            )
    assert seen == guarded, f"not all workload steps were found: {guarded - seen}"


def test_an_aborted_migration_discards_its_staging_claim() -> None:
    """A staging PVC left behind by an abort holds a Longhorn volume nobody will reclaim."""
    always = _names(_migration_block()["always"])
    assert (
        "Discard the staging claim after an abort that never touched the original"
        in always
    ), (
        "an abort before the original is deleted must clean up the staging claim, or every "
        "failed run leaks a PVC — n8n-files leaked one on 2026-08-20"
    )


def test_the_staging_discard_is_skipped_once_the_original_is_gone() -> None:
    """After the delete, staging is the only copy — an abort must leave it standing."""
    always = _migration_block()["always"]
    discard = next(
        t
        for t in always
        if t.get("name")
        == "Discard the staging claim after an abort that never touched the original"
    )
    assert "mig_original_deleted" in str(discard.get("when", "")), (
        "the discard must be conditional on the original still existing; unconditionally "
        "deleting the staging claim in `always` destroys the only copy of the data"
    )
    names = _names(_migration_block()["block"])
    assert names.index("Delete the original claim") < names.index(
        "Record that the original is gone"
    ), "the flag has to be set after the delete it describes"
