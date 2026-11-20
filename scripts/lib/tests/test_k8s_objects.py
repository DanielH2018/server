from lib.k8s_objects import (
    BackupTarget,
    Condition,
    ConditionStatus,
    K8sObject,
    LonghornVolume,
    VolumeState,
    parse_conditions,
)
from lib.longhorn_lib.longhorn_backups import GROUP_LABEL_PREFIX


def test_conditions_are_keyed_by_type_and_the_first_of_a_type_wins():
    item = {
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "Done", "message": "ok"},
                {"type": "Ready", "status": "False", "message": "later"},
                None,
                {"type": "Stalled", "status": "maybe"},
            ]
        }
    }
    assert parse_conditions(item) == {
        "Ready": Condition(ConditionStatus.TRUE, "Done", "ok"),
        "Stalled": Condition(ConditionStatus.UNKNOWN, "", ""),
    }


def test_an_object_reads_its_metadata_and_a_missing_field_as_empty():
    obj = K8sObject.from_item(
        {
            "kind": "Volume",
            "metadata": {
                "name": "pvc-a",
                "namespace": "longhorn-system",
                "labels": None,
            },
        }
    )
    assert (obj.kind, obj.name, obj.namespace) == ("Volume", "pvc-a", "longhorn-system")
    assert (obj.labels, obj.annotations, obj.status, obj.conditions) == ({}, {}, {}, {})


def test_a_volume_keeps_the_last_group_label_and_an_unknown_state_reads_unknown():
    labels = dict.fromkeys(
        [GROUP_LABEL_PREFIX + "default", GROUP_LABEL_PREFIX + "weekly-backup-d3"],
        "enabled",
    )
    volume = LonghornVolume.from_item(
        {
            "metadata": {"name": "pvc-a", "labels": labels},
            "status": {"state": "faulted"},
        }
    )
    assert volume.groups == ("default", "weekly-backup-d3")
    assert volume.group == "weekly-backup-d3"
    assert volume.state is VolumeState.UNKNOWN


def test_a_volume_with_no_group_label_has_no_group():
    volume = LonghornVolume.from_item(
        {
            "metadata": {"name": "pvc-a", "labels": {"app": "x"}},
            "status": {"state": "attached"},
        }
    )
    assert volume.group is None
    assert volume.state is VolumeState.ATTACHED


def test_a_blank_target_url_reads_disarmed_and_only_a_true_available_counts():
    target = BackupTarget.from_item(
        {
            "metadata": {"name": "default"},
            "spec": {"backupTargetURL": "   "},
            "status": {"available": "true"},
        }
    )
    assert target == BackupTarget("default", None, False, "")


def test_an_armed_target_reports_its_first_condition_message():
    target = BackupTarget.from_item(
        {
            "metadata": {"name": "r2"},
            "spec": {"backupTargetURL": "s3://bucket@auto/"},
            "status": {
                "available": False,
                "conditions": [
                    {
                        "type": "Unavailable",
                        "status": "True",
                        "message": "403 Forbidden",
                    },
                    {"type": "Other", "status": "True", "message": "second"},
                ],
            },
        }
    )
    assert target == BackupTarget("r2", "s3://bucket@auto/", False, "403 Forbidden")
