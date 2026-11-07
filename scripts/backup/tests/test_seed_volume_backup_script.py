"""The seed script's refusals and what it creates, against a fake cluster."""

import json
from datetime import datetime, timezone

import pytest
import seed_volume_backup as seed
from lib.exit_codes import FAILED, OK

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
GROUP_LABEL = "recurring-job-group.longhorn.io/weekly-backup-d3"
ARGS = ["--claim", "app-config"]
UUID_NAME = "seed-1234"


class Cluster:
    """A cluster answering the seed's reads, with each field overridable by a test."""

    def __init__(self, **over):
        self.pv = "pvc-1234"
        self.labels = {GROUP_LABEL: "enabled"}
        self.target_name = "r2"
        self.url = "s3://bucket@auto/"
        self.available = True
        self.backups = []
        self.jobs = [
            {
                "metadata": {"name": "weekly-d3"},
                "spec": {"groups": ["weekly-backup-d3"]},
            }
        ]
        self.backup_state = {"state": "Completed"}
        self.ready = True
        self.applied = []
        self.__dict__.update(over)

    def kubectl(self, *args, stdin=None):
        if args[:2] == ("apply", "-f"):
            self.applied.append(json.loads(stdin or ""))
            return ""
        if "pvc" in args:
            return self.pv
        kind = args[3]
        named = len(args) > 6
        if kind == "volumes.longhorn.io":
            return json.dumps(
                {
                    "metadata": {"labels": self.labels},
                    "spec": {"backupTargetName": self.target_name},
                    "status": {"robustness": "healthy"},
                }
            )
        if kind == "backuptargets.longhorn.io":
            return json.dumps(
                {
                    "spec": {"backupTargetURL": self.url},
                    "status": {"available": self.available},
                }
            )
        if kind == "recurringjobs.longhorn.io":
            return json.dumps({"items": self.jobs})
        if kind == "snapshots.longhorn.io":
            return json.dumps({"status": {"readyToUse": self.ready}})
        if kind == "backups.longhorn.io":
            if named:
                return json.dumps({"status": self.backup_state})
            return json.dumps({"items": self.backups})
        raise AssertionError(args)


def run(cluster: Cluster, argv: list[str]) -> int:
    return seed.main(argv, cluster.kubectl, sleep=lambda _: None, now=lambda: NOW)


def test_an_apply_creates_a_snapshot_then_a_labelled_backup():
    cluster = Cluster()
    assert run(cluster, [*ARGS, "--apply"]) == OK
    snapshot, backup = cluster.applied
    assert snapshot["kind"] == "Snapshot"
    assert snapshot["metadata"]["name"] == UUID_NAME
    assert snapshot["spec"]["labels"]["RecurringJob"] == "weekly-d3"
    assert backup["kind"] == "Backup"
    assert backup["metadata"]["labels"] == {
        "backup-target": "r2",
        "backup-volume": "pvc-1234",
    }
    assert backup["spec"]["snapshotName"] == UUID_NAME
    assert backup["spec"]["labels"]["RecurringJob"] == "weekly-d3"


def test_a_dry_run_creates_nothing_and_says_what_it_would_seed(capsys):
    cluster = Cluster()
    assert run(cluster, ARGS) == OK
    assert cluster.applied == []
    assert "weekly-d3" in capsys.readouterr().out


@pytest.mark.parametrize(
    "over, message",
    [
        ({"pv": ""}, "not bound"),
        ({"labels": {}}, "(none)"),
        (
            {"labels": {"recurring-job-group.longhorn.io/no-backup": "enabled"}},
            "no-backup",
        ),
        ({"url": ""}, "disarmed"),
        ({"available": False}, "unavailable"),
        ({"backups": [{"status": {"volumeName": "pvc-1234"}}]}, "already has 1 backup"),
        ({"jobs": []}, "no RecurringJob"),
    ],
    ids=[
        "unbound",
        "no-tier",
        "no-backup",
        "disarmed",
        "unavailable",
        "existing",
        "no-job",
    ],
)
def test_each_refusal_changes_nothing_and_names_its_cause(over, message, capsys):
    cluster = Cluster(**over)
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert cluster.applied == []
    err = capsys.readouterr().err
    assert message in err
    assert "Nothing has been changed" in err


def test_a_backup_of_another_volume_does_not_count_as_existing():
    cluster = Cluster(backups=[{"status": {"volumeName": "pvc-other"}}])
    assert run(cluster, ARGS) == OK


def test_a_forced_seed_takes_a_timestamped_name():
    cluster = Cluster(backups=[{"status": {"volumeName": "pvc-1234"}}])
    assert run(cluster, [*ARGS, "--allow-existing", "--apply"]) == OK
    assert cluster.applied[0]["metadata"]["name"] == f"{UUID_NAME}-20261107120000"


def test_a_missing_target_name_means_the_b2_target():
    cluster = Cluster(target_name=None)
    assert run(cluster, [*ARGS, "--apply"]) == OK
    assert cluster.applied[1]["metadata"]["labels"]["backup-target"] == "default"


def test_a_snapshot_that_never_readies_requests_no_backup(capsys):
    cluster = Cluster(ready=False)
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert [m["kind"] for m in cluster.applied] == ["Snapshot"]
    assert "No Backup was requested" in capsys.readouterr().err


def test_a_backup_in_error_fails_and_names_the_cr_to_delete(capsys):
    cluster = Cluster(backup_state={"state": "Error", "error": "target refused"})
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    err = capsys.readouterr().err
    assert "target refused" in err and UUID_NAME in err


def test_a_backup_that_never_finishes_fails(capsys):
    cluster = Cluster(backup_state={"state": "InProgress"})
    assert run(cluster, [*ARGS, "--apply"]) == FAILED
    assert "did not finish" in capsys.readouterr().err


def test_the_daily_tier_resolves_through_the_job_not_the_group_name():
    jobs = [{"metadata": {"name": "daily-backup"}, "spec": {"groups": ["default"]}}]
    assert seed.owning_job(jobs, "default") == "daily-backup"
    assert seed.owning_job(jobs, "weekly-backup-d3") is None
