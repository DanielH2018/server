"""The label tasks render the same group labels the live cluster carries (#3946).

`tasks/longhorn.yml` and `tasks/longhorn-weekly-shard.yml` take every group label from the
`longhorn_groups` filters, which take them from `files/longhorn_backups.py`. A one-byte change
in a rendered label renames a live group: the volume drops out of its RecurringJob and nothing
fails. So this renders each label task for every volume in the tier lists and every shard, and
compares the labels against strings typed out here. They are deliberately NOT built from the
module's constants: an expectation derived from the same constant as the render would follow a
rename and pass.
"""

import re

import pytest
from jinja2 import StrictUndefined

from _helpers import load_defaults, load_tasks
from lib.ansible_jinja_env import make_ansible_env
from longhorn_groups import (
    backup_group_label,
    longhorn_backup_name,
    weekly_backup_group,
    weekly_backup_shard,
)
from lib.repo_paths import K3S_ROLE

LABEL = re.compile(r"recurring-job-group\.longhorn\.io/\S+")
UNLISTED = "homelab/not-in-any-tier-list"

# Each label task, by name, and the labels its command writes or selects on. `{weekday}` is the
# volume's index in k3s_longhorn_weekly_volumes mod 7; `{shard}` is the included shard.
EXPECTED = {
    "Find backed-up Longhorn volumes that should be excluded": [
        "recurring-job-group.longhorn.io/default=enabled",
    ],
    "Move excluded volumes into the no-backup recurring-job group": [
        "recurring-job-group.longhorn.io/no-backup=enabled",
        "recurring-job-group.longhorn.io/default-",
    ],
    "Find excluded Longhorn volumes that should be backed up again": [
        "recurring-job-group.longhorn.io/no-backup=enabled",
    ],
    "Return volumes to the default recurring-job group": [
        "recurring-job-group.longhorn.io/default=enabled",
        "recurring-job-group.longhorn.io/no-backup-",
    ],
    "Move weekly-tier volumes out of the daily group": [
        "recurring-job-group.longhorn.io/weekly-backup-d{weekday}=enabled",
        "recurring-job-group.longhorn.io/default-",
    ],
    "Find volumes still carrying the legacy weekly-backup label": [
        "recurring-job-group.longhorn.io/weekly-backup=enabled",
    ],
    "Migrate legacy weekly-backup labels to their weekday shard": [
        "recurring-job-group.longhorn.io/weekly-backup-d{weekday}=enabled",
        "recurring-job-group.longhorn.io/weekly-backup-",
    ],
    "Return legacy weekly-tier volumes to the default recurring-job group": [
        "recurring-job-group.longhorn.io/default=enabled",
        "recurring-job-group.longhorn.io/weekly-backup-",
    ],
    "Find volumes in weekly shard d{{ shard }}": [
        "recurring-job-group.longhorn.io/weekly-backup-d{shard}=enabled",
    ],
    "Return de-listed volumes to the default recurring-job group from shard d{{ shard }}": [
        "recurring-job-group.longhorn.io/default=enabled",
        "recurring-job-group.longhorn.io/weekly-backup-d{shard}-",
    ],
    "Move volumes whose weekday changed out of shard d{{ shard }}": [
        "recurring-job-group.longhorn.io/weekly-backup-d{weekday}=enabled",
        "recurring-job-group.longhorn.io/weekly-backup-d{shard}-",
    ],
}

ENV = make_ansible_env(undefined_cls=StrictUndefined)


def _command_text(task: dict) -> str:
    command = task["ansible.builtin.command"]
    return command["cmd"] if "cmd" in command else " ".join(command["argv"])


def _label_tasks() -> list[dict]:
    tasks = load_tasks(K3S_ROLE / "tasks" / "longhorn.yml") + load_tasks(
        K3S_ROLE / "tasks" / "longhorn-weekly-shard.yml"
    )
    return [
        t
        for t in tasks
        if "ansible.builtin.command" in t
        and re.search(r"backup_group_label|recurring-job-group", _command_text(t))
    ]


def _passes_when(task: dict, context: dict) -> bool:
    conditions = task.get("when", [])
    if isinstance(conditions, str):
        conditions = [conditions]
    return all(
        ENV.from_string("{{ (%s) | bool }}" % c).render(context) == "True"
        for c in conditions
    )


def _cases(defaults: dict):
    weekly = defaults["k3s_longhorn_weekly_volumes"]
    volumes = [*weekly, *defaults["k3s_longhorn_nobackup_volumes"], UNLISTED]
    for pvc in volumes:
        for shard in range(7):
            yield pvc, shard


def test_every_label_task_renders_the_live_labels():
    defaults = load_defaults(K3S_ROLE)
    weekly = defaults["k3s_longhorn_weekly_volumes"]
    tasks = _label_tasks()
    assert {t["name"] for t in tasks} == set(EXPECTED), (
        "a label task was added, renamed or dropped; give it its typed-out labels here"
    )
    for task in tasks:
        assert "recurring-job-group" not in _command_text(task), (
            f"{task['name']} spells a group label out; use backup_group_label (#3946)"
        )
    rendered = 0
    for task in tasks:
        for pvc, shard in _cases(defaults):
            context = {
                "item": f"pvc-0000 {pvc}",
                "shard": shard,
                "k3s_longhorn_weekly_volumes": weekly,
                "k3s_longhorn_nobackup_volumes": defaults[
                    "k3s_longhorn_nobackup_volumes"
                ],
                "k3s_longhorn_backup_class_pvcs": {"stdout_lines": [pvc]},
            }
            if not _passes_when(task, context):
                continue
            text = ENV.from_string(_command_text(task)).render(context)
            weekday = weekly.index(pvc) % 7 if pvc in weekly else None
            expected = [
                e.format(shard=shard, weekday=weekday) for e in EXPECTED[task["name"]]
            ]
            assert LABEL.findall(text) == expected, (task["name"], pvc, shard)
            rendered += 1
    # Every weekly volume reaches the weekday-shard tasks, so the count sits well above the
    # number of tasks; a `when` that stopped matching would drop it toward zero.
    assert rendered >= len(weekly) * 7


def test_a_weekly_volume_shards_on_its_first_index():
    volumes = ["a", "b", "a", "c", "d", "e", "f", "g", "h"]
    assert weekly_backup_shard("a", volumes) == 0
    assert weekly_backup_shard("h", volumes) == 1


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
