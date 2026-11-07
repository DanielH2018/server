"""The group-label filters and the BackupTarget templates render the live names (#3946).

The label TASKS that took their labels from the `longhorn_groups` filters moved to the
`longhorn-reconcile` role's `files/longhorn_reconcile.py` at H1.6, and the two R2 routing tasks
followed at H1.7a. Their typed-out cases moved with them
(`ansible/roles/k8s/longhorn-reconcile/tests/`). What stays here is the filters the reconcile
ConfigMap and the RecurringJob template still call, and the templates that write the
BackupTarget names (the `longhorn-backup` role's, since H1.7b). The expectations are typed out,
not built from the module's constants: an expectation derived from the same constant as the
render would follow a rename and pass.
"""

import pytest

from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from longhorn_groups import (
    backup_group_label,
    longhorn_backup_name,
    longhorn_nobackup_claims,
    longhorn_weekly_claims,
    weekly_backup_group,
    weekly_backup_shard,
)
from lib.repo_paths import K8S_ROLES

BACKUP_TEMPLATES = K8S_ROLES / "longhorn-backup" / "templates"


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


def test_the_backup_target_templates_name_the_live_targets():
    """The `longhorn-backup` role's two target templates render the names the cluster holds."""
    env = make_ansible_env([BACKUP_TEMPLATES])
    context = {
        "k3s_longhorn_backup_armed": False,
        "k3s_longhorn_r2_armed": False,
        "k3s_longhorn_backupstore_poll_interval": 0,
        "k3s_longhorn_r2_poll_interval": 0,
    }
    names = {
        template: yaml_fast.safe_load(env.get_template(template).render(**context))[
            "metadata"
        ]["name"]
        for template in (
            "longhorn-backuptarget-default.yaml.j2",
            "longhorn-backuptarget-r2.yaml.j2",
        )
    }
    assert names == {
        "longhorn-backuptarget-default.yaml.j2": "default",
        "longhorn-backuptarget-r2.yaml.j2": "r2",
    }
