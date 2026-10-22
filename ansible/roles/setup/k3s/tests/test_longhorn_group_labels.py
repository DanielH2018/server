"""The group-label filters and the R2 routing tasks render the live names (#3946).

The label TASKS that took their labels from the `longhorn_groups` filters moved to the
`longhorn-reconcile` role's `files/longhorn_reconcile.py` at H1.6, and their typed-out label
cases moved with them (`ansible/roles/k8s/longhorn-reconcile/tests/`). What stays here is the
filters those tasks and the RecurringJob template still call, the tasks that write the
BackupTarget names, and the two R2 routing tasks until H1.7a deletes them. The expectations are
typed out, not built from the module's constants: an expectation derived from the same constant
as the render would follow a rename and pass.
"""

import re

import pytest
from jinja2 import StrictUndefined

from _helpers import load_tasks
from lib.ansible_jinja_env import make_ansible_env
from longhorn_groups import (
    backup_group_label,
    longhorn_backup_name,
    longhorn_nobackup_claims,
    longhorn_weekly_claims,
    weekly_backup_group,
    weekly_backup_shard,
)
from lib.repo_paths import K3S_ROLE

ENV = make_ansible_env(undefined_cls=StrictUndefined)


def _command_text(task: dict) -> str:
    command = task["ansible.builtin.command"]
    return command["cmd"] if "cmd" in command else " ".join(command["argv"])


def _passes_when(task: dict, context: dict) -> bool:
    conditions = task.get("when", [])
    if isinstance(conditions, str):
        conditions = [conditions]
    return all(
        ENV.from_string("{{ (%s) | bool }}" % c).render(context) == "True"
        for c in conditions
    )


def test_a_weekly_volume_shards_on_the_shard_its_entry_declares():
    entries = [
        {"name": "a", "weekly_backup_claims": {"a-config": 3, "a-data": 0}},
        {"name": "b", "namespace": "media", "weekly_backup_claims": {"b-config": 6}},
        {"name": "c", "no_backup_claims": ["c-cache"]},
    ]
    weekly = longhorn_weekly_claims(entries, "homelab")
    assert weekly == {"homelab/a-config": 3, "homelab/a-data": 0, "media/b-config": 6}
    assert weekly_backup_shard("homelab/a-config", weekly) == 3
    assert longhorn_nobackup_claims(entries, "homelab") == ["homelab/c-cache"]
    with pytest.raises(ValueError, match="not a weekly-tier volume"):
        weekly_backup_shard("homelab/c-cache", weekly)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            [{"name": "a", "weekly_backup_claims": {"a-config": 7}}],
            "not an integer 0-6",
        ),
        ([{"name": "a", "weekly_backup_claims": {"a-config": True}}], "not an integer"),
        ([{"name": "a", "weekly_backup_claims": {"a-config": "1"}}], "not an integer"),
        (
            [
                {"name": "a", "weekly_backup_claims": {"shared": 1}},
                {"name": "b", "weekly_backup_claims": {"shared": 2}},
            ],
            "declared twice",
        ),
    ],
)
def test_a_shard_a_volume_cannot_be_placed_on_is_refused(entries, message):
    with pytest.raises(ValueError, match=message):
        longhorn_weekly_claims(entries, "homelab")


def test_an_unknown_group_is_refused():
    assert (
        backup_group_label("no-backup") == "recurring-job-group.longhorn.io/no-backup"
    )
    with pytest.raises(ValueError, match="not a Longhorn backup group"):
        backup_group_label("no_backup")


def test_a_shard_outside_the_week_is_refused():
    assert weekly_backup_group(6) == "weekly-backup-d6"
    with pytest.raises(ValueError, match="outside 0-6"):
        weekly_backup_group(7)


def test_the_bare_names_are_the_live_ones():
    """The drill and seed playbook match on these; typed out, as the labels above are."""
    assert {
        key: longhorn_backup_name(key)
        for key in (
            "label_prefix",
            "default_group",
            "no_backup_group",
            "weekly_legacy_group",
            "b2_target",
            "r2_target",
        )
    } == {
        "label_prefix": "recurring-job-group.longhorn.io/",
        "default_group": "default",
        "no_backup_group": "no-backup",
        "weekly_legacy_group": "weekly-backup",
        "b2_target": "default",
        "r2_target": "r2",
    }
    with pytest.raises(ValueError, match="not a Longhorn backup name"):
        longhorn_backup_name("no-backup")


def _backup_task(name: str) -> dict:
    tasks = load_tasks(K3S_ROLE / "tasks" / "longhorn-backup.yml")
    return next(t for t in tasks if t["name"] == name)


@pytest.mark.parametrize(
    ("name", "live", "patch"),
    [
        (
            "Route the selected volumes to R2",
            "pvc-0000 homelab/listed default",
            '{"spec":{"backupTargetName":"r2"}}',
        ),
        (
            "Return de-listed volumes to the default backup target",
            "pvc-0000 homelab/unlisted r2",
            '{"spec":{"backupTargetName":"default"}}',
        ),
    ],
)
def test_the_r2_routing_writes_the_live_target_names(name, live, patch):
    """These tasks WRITE spec.backupTargetName, which the health cron and the drill read."""
    task = _backup_task(name)
    context = {"k3s_longhorn_r2_volumes": ["homelab/listed"]}
    assert _passes_when(task, {**context, "item": live})
    # Already where it belongs: the route skips itself, so a converged cluster changes nothing.
    settled = (
        "pvc-0000 homelab/listed r2" if "R2" in name else "pvc-0000 homelab/unlisted"
    )
    assert not _passes_when(task, {**context, "item": settled})
    assert ENV.from_string(task["vars"][next(iter(task["vars"]))]).render() == patch


def test_the_b2_target_tasks_name_the_live_target():
    tasks = load_tasks(K3S_ROLE / "tasks" / "longhorn-backup.yml")
    # Up to `--type`: the patch body after it goes through Ansible's `quote`, which ENV lacks.
    commands = [
        ENV.from_string(_command_text(t).split("--type")[0]).render()
        for t in tasks
        if "ansible.builtin.command" in t
        and re.search(r"backuptargets?(\.longhorn\.io)? ", _command_text(t))
    ]
    assert len(commands) == 4, commands
    for text in commands:
        assert re.search(r"backuptargets?(\.longhorn\.io)? default ", text), text
