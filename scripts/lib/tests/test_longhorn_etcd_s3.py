"""Check 12 of the backup heartbeat: k3s's S3 etcd snapshots must stay fresh and armed.

The reader-side half is in `hosts/components/k3s-host-checks/tests/test_longhorn_backup_health_reader.py`,
and `ansible/tests/setup/test_k3s_server_component.py` runs the parser over the rendered drop-in.
"""

import pytest
from longhorn_lib import longhorn_etcd_s3_logic as logic

NOW = 1_800_000_000.0
DROP_IN = 'etcd-s3: true\netcd-snapshot-schedule-cron: "0 */12 * * *"\n'
ALLOWED_S = 14 * 3600


def _rfc3339(epoch: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _snapshot(
    age_s: float, location: str = "s3://b/offbox", ready: bool = True
) -> dict:
    return {
        "spec": {"location": location},
        "status": {"creationTime": _rfc3339(NOW - age_s), "readyToUse": ready},
    }


def _raises(err: Exception):
    def read() -> str:
        raise err

    return read


def test_an_armed_drop_in_is_clean_and_derives_the_bound():
    assert logic.allowed_age_s(lambda: DROP_IN) == ALLOWED_S


@pytest.mark.parametrize(
    ("schedule", "period_h"),
    [("0 */12 * * *", 12), ("15 */6 * * *", 6), ("45 2 * * *", 24), ("0 */5 * * *", 5)],
)
def test_the_period_follows_the_schedule(schedule, period_h):
    assert logic.schedule_period_s(schedule) == period_h * 3600


@pytest.mark.parametrize(
    "schedule", ["", "@daily", "0 0,12 * * *", "0 */0 * * *", "0 3 * * 1", "0 */12 * *"]
)
def test_a_schedule_with_no_single_period_is_flagged(schedule):
    problem = logic.allowed_age_s(
        lambda: f"etcd-s3: true\netcd-snapshot-schedule-cron: '{schedule}'\n"
    )
    assert isinstance(problem, tuple)
    assert "cannot derive an S3 snapshot age bound" in problem[1]


@pytest.mark.parametrize(
    ("read", "named"),
    [
        (lambda: DROP_IN.replace("true", "false"), "disarmed (etcd-s3: false)"),
        (lambda: 'etcd-snapshot-schedule-cron: "0 0 * * *"\n', "no etcd-s3 key"),
        (lambda: "etcd-s3: true\n", "no etcd-snapshot-schedule-cron key"),
        (_raises(FileNotFoundError("gone")), "drop-in missing"),
        (_raises(NotADirectoryError("gone")), "drop-in missing"),
        (_raises(PermissionError("denied")), "drop-in unreadable"),
    ],
)
def test_a_drop_in_fault_is_flagged(read, named):
    problem = logic.allowed_age_s(read)
    assert isinstance(problem, tuple)
    assert named in problem[1]


def test_a_nested_key_is_not_read_as_a_top_level_one():
    assert logic.parse_drop_in("disable:\n  - etcd-s3: true\n") == {"disable": ""}


def test_a_snapshot_inside_the_bound_is_clean():
    problem, age_s = logic.check_newest_snapshot(
        [_snapshot(ALLOWED_S), _snapshot(ALLOWED_S + 3600)], ALLOWED_S, NOW
    )
    assert problem is None
    assert age_s == ALLOWED_S


def test_a_snapshot_past_the_bound_is_flagged():
    problem, _ = logic.check_newest_snapshot(
        [_snapshot(ALLOWED_S + 3600)], ALLOWED_S, NOW
    )
    assert problem
    assert "newest S3 etcd snapshot is 15h old (limit 14h" in problem[1]


@pytest.mark.parametrize(
    "items",
    [
        [],
        [_snapshot(60, location="file:///var/lib/rancher/k3s/server/db/snapshots/a")],
        [_snapshot(60, ready=False)],
        [{"spec": {"location": "s3://b/offbox"}, "status": {"readyToUse": True}}],
    ],
)
def test_no_usable_s3_snapshot_is_flagged(items):
    problem, _ = logic.check_newest_snapshot(items, ALLOWED_S, NOW)
    assert problem
    assert "no ready etcd snapshot with an S3 location exists" in problem[1]
