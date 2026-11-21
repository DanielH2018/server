"""The cluster-node units container case's verdict, without a container.

The case itself runs only in CI's `host_packages` job, once per Ubuntu release. This holds what
it decides from the container's output, with a green and a red input for each thing it reads,
and holds that the job runs it.

Run: uv run pytest scripts/validate/tests/test_node_units_case.py
"""

from validate import host_package_cases
from validate.validate_lib import case_groups, node_units_case
from validate.validate_lib.node_units_case import nut_monitor_problems

RUNNING = {
    "ActiveState": "active",
    "SubState": "running",
    "Result": "success",
    "ExecMainStatus": "0",
    "NRestarts": "0",
}


def test_a_running_upsmon_daemon_is_clean():
    assert nut_monitor_problems(RUNNING, "/usr/sbin/upsmon") == []
    assert nut_monitor_problems(RUNNING, "/usr/lib/nut/upsmon") == []


def test_a_daemon_that_never_executed_is_flagged():
    failed = {
        **RUNNING,
        "ActiveState": "failed",
        "SubState": "failed",
        "Result": "exit-code",
        "ExecMainStatus": "203",
    }
    assert nut_monitor_problems(failed, "") == [
        "nut-monitor is failed/failed (result exit-code, exit status 203)",
        "nut-monitor's main process runs nothing, not upsmon",
    ]


def test_the_mode_wrapper_or_a_restart_loop_is_flagged():
    assert nut_monitor_problems(RUNNING, "/usr/bin/dash") == [
        "nut-monitor's main process runs /usr/bin/dash, not upsmon"
    ]
    assert nut_monitor_problems({**RUNNING, "NRestarts": "2"}, "/usr/sbin/upsmon") == [
        "nut-monitor restarted 2 times"
    ]


def test_the_case_starts_the_shipped_drop_in():
    assert node_units_case.NUT_DROPIN_SOURCE.is_file()


def test_the_host_packages_job_runs_the_case():
    runs = [run for _, run in case_groups.groups(host_package_cases.Cases)]
    assert node_units_case.run in runs
