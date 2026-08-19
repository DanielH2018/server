#!/usr/bin/env python3
"""Guards on the playbook that deletes a migrated volume's old backup chain.

Deleting a backup chain is irreversible and the objects it removes are, for a window, the only
copy of that volume's data. Two properties keep that safe, and both are easy to lose in an edit
that looks like a simplification.

  THE REPLACEMENT MUST ALREADY HAVE A BACKUP. A migrated volume starts with none: its old chain
  belongs to a Longhorn volume that no longer exists. Deleting the chain before the replacement
  has been backed up leaves the service with no recovery point at all. The check is made by the
  playbook rather than by the operator remembering.

  ONLY ORPHANS, ONLY THIS CLAIM. A backup whose volume still exists is somebody's live chain.
  Longhorn does not record the PVC on the Backup object directly — it is inside the
  KubernetesStatus label, a JSON string — so the selection has to reach into that, and reaching
  into it with a bare substring would let `n8n-data` also select `n8n-data-something`.

Run: uv run pytest ansible/tests/test_drop_migrated_backup_chain.py
"""

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
PLAY = ANSIBLE / "drop_migrated_backup_chain.yml"


def _tasks() -> list:
    return yaml.safe_load(PLAY.read_text())[0]["tasks"]


def _names() -> list[str]:
    return [t["name"] for t in _tasks() if isinstance(t, dict) and "name" in t]


def test_nothing_is_deleted_before_the_replacement_has_a_backup() -> None:
    """A migrated volume has no backup of its own until its weekday shard runs."""
    names = _names()
    check = names.index(
        "Refuse to drop the old chain until the replacement has one of its own"
    )
    delete = names.index("Delete the stranded backups")
    assert check < delete, (
        "the chain is deleted before the replacement is confirmed to have a backup, which would "
        "leave the volume with no recovery point at all"
    )


def test_only_backups_of_vanished_volumes_are_considered() -> None:
    """A backup whose volume still exists is a live chain, not debris."""
    text = PLAY.read_text()
    assert "rejectattr('status.volumeName', 'in'," in text, (
        "the candidate set must exclude backups whose Longhorn volume still exists"
    )
    assert "map('trim')" in text, (
        "kubectl custom-columns pads names to the column width, and a padded name matches "
        "nothing — every backup would then classify as an orphan"
    )
    assert "Refuse to classify orphans against an unusable volume list" in text, (
        "an empty or malformed volume list makes the orphan test vacuously true, so it must "
        "be proven usable before anything is classified"
    )
    assert "'equalto', 'Completed'" in text, (
        "only Completed backups belong here; Error backups are the reaper's path"
    )


def test_the_claim_match_is_anchored_on_the_quoted_field() -> None:
    """A bare substring would let one claim's name select another's backups."""
    text = PLAY.read_text()
    assert '"pvcName":"' in text, (
        "match the quoted key/value pair inside KubernetesStatus, not the bare claim name — "
        "otherwise n8n-data also selects n8n-data-something"
    )


def test_the_cost_of_deleting_through_longhorn_is_written_down() -> None:
    """The list reads as cheap because it is short; the cost is per block, not per object."""
    text = PLAY.read_text()
    assert "Class C" in text and "1.28" in text, (
        "the per-block deletion cost is the reason this playbook exists instead of the reaper, "
        "and it has to survive someone reading only the header"
    )
