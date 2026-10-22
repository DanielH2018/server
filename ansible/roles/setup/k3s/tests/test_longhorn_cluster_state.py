"""The pure readers of BackupTarget and RecurringJob lists (H1.7a).

`test_longhorn_host_readers_follow_the_cluster.py` runs the whole reader against a stub kubectl.
These cases pin the decisions it rests on, without a subprocess.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_longhorn_cluster_state.py
"""

import pytest
from longhorn_lib import longhorn_cluster_state as state


def _target(name, url, available=True, message=""):
    status = {"available": available}
    if message:
        status["conditions"] = [{"message": message}]
    return {
        "metadata": {"name": name},
        "spec": {"backupTargetURL": url},
        "status": status,
    }


@pytest.mark.parametrize(
    ("url", "armed"),
    [("s3://b@r/p", True), ("", False), ("  ", False), (None, False)],
)
def test_a_target_is_armed_while_its_url_holds_anything_but_whitespace(url, armed):
    assert state.target_state(_target("default", url))["armed"] is armed


def test_availability_is_the_string_the_check_compares_and_a_missing_one_is_not_true():
    assert state.target_state(_target("d", "u", True))["raw"] == "true"
    assert state.target_state(_target("d", "u", False))["raw"] == "false"
    assert state.target_state({"spec": {"backupTargetURL": "u"}})["raw"] == "unreported"


def test_the_reason_is_the_first_condition_cut_to_160_characters():
    long = _target("d", "u", False, "x" * 400)
    assert state.target_state(long)["reason"] == "x" * 160


def _both(default_url, r2_url):
    return state.targets_by_name(
        [_target("default", default_url), _target("r2", r2_url)]
    )


def test_a_blank_url_disarms_exactly_that_target():
    assert state.watch_plan(_both("", "u")) == (["r2"], ["default"], [])
    assert state.watch_plan(_both("u", "")) == (["default"], ["r2"], [])
    assert state.watch_plan(_both("u", "u")) == (["default", "r2"], [], [])


def test_a_list_that_failed_to_arrive_disarms_nothing_and_adds_no_second_problem():
    assert state.watch_plan(None) == (["default", "r2"], [], [])


def test_a_target_missing_from_a_list_that_arrived_stays_watched_and_is_a_problem():
    only_r2 = state.targets_by_name([_target("r2", "u")])
    watched, disarmed, problems = state.watch_plan(only_r2)
    assert watched == ["default", "r2"]
    assert disarmed == []
    assert [rank for rank, _ in problems] == [1]
    assert "backup target default is not in the cluster" in problems[0][1]


def test_run_times_come_from_the_first_two_fields_of_each_jobs_cron():
    crons = {"daily-backup": "30 3 * * *"} | {
        f"weekly-backup-d{day}": f"15 4 * * {day}" for day in range(7)
    }
    times = state.run_times(crons)
    assert times["daily-backup"] == "3:30"
    assert times["weekly-backup-d6"] == "4:15"
    assert state.legacy_run_time(times) == "4:15"


def test_a_missing_or_malformed_cron_is_none_for_that_job_only():
    crons = {"daily-backup": "30", "weekly-backup-d1": "15 4 * * 1"}
    times = state.run_times(crons)
    assert times["daily-backup"] is None
    assert times["weekly-backup-d0"] is None
    assert times["weekly-backup-d1"] == "4:15"
    assert state.legacy_run_time(times) == "4:15"
    assert state.legacy_run_time(state.run_times({})) is None
