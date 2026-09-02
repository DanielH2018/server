#!/usr/bin/env python3
"""Guards on the playbook that retires seed backups once the rotation covers their volume.

A seed is the only recovery point a volume has until its weekday shard has produced enough
backups of its own, and deleting it is irreversible. Three properties keep the play safe, and
each is a one-line edit away from being lost.

  THE ROTATION FLOOR COMES BEFORE THE DELETE. A seed goes only when its volume already holds
  `drop_seed_floor` Completed backups carrying a RecurringJob label — the state retain keeps.

  ONLY SEEDS. The selection needs both the `seed-` name and the absent RecurringJob label; either
  alone matches something that is not a seed.

  DRY RUN BY DEFAULT. The delete task is gated on `drop_seed_apply`, which defaults to false.

Run: uv run pytest ansible/tests/longhorn/test_drop_seed_backups.py
"""

from _helpers import ANSIBLE
from _helpers import load_yaml


PLAY = ANSIBLE / "drop_seed_backups.yml"


def _play() -> dict:
    return load_yaml(PLAY)[0]


def _tasks() -> list:
    return _play()["tasks"]


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t.get("name") == name)


def _names() -> list[str]:
    return [t["name"] for t in _tasks() if isinstance(t, dict) and "name" in t]


def test_floor_is_applied_before_the_delete() -> None:
    names = _names()
    assert names.index(
        "Split the seeds into superseded and still-covering"
    ) < names.index("Delete the superseded seeds"), (
        "a seed would be deleted before its volume's rotation is checked"
    )


def test_floor_counts_only_rotation_backups() -> None:
    """A seed must not count toward the floor that retires it."""
    when = _task("Split the seeds into superseded and still-covering")["when"]
    assert "selectattr('status.labels.RecurringJob', 'defined')" in when
    assert "drop_seed_floor" in when


def test_floor_defaults_to_the_shard_retain() -> None:
    assert _play()["vars"]["drop_seed_floor"] == 2


def test_selection_requires_both_seed_markers() -> None:
    fact = _task("Narrow to seeds on volumes that still exist")[
        "ansible.builtin.set_fact"
    ]["drop_seed_candidates"]
    assert "'match', '^seed-'" in fact, "the seed-name prefix is not required"
    assert "rejectattr('status.labels.RecurringJob', 'defined')" in fact, (
        "a labelled backup could be selected as a seed"
    )
    assert "drop_seed_live_volumes" in fact, (
        "a seed of a vanished volume is the migration's chain, not this play's"
    )


def test_delete_is_gated_on_apply_and_apply_defaults_off() -> None:
    assert _play()["vars"]["drop_seed_apply"] is False
    assert "drop_seed_apply" in _task("Delete the superseded seeds")["when"]


def test_delete_loops_only_over_the_superseded_list() -> None:
    assert "drop_seed_superseded" in _task("Delete the superseded seeds")["loop"]
