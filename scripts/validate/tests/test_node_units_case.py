"""The cluster-node units container case's verdict, without a container.

The case itself runs only in CI's `host_packages` job, once per Ubuntu release. This holds what
it decides from the container's output, with a green and a red input for each thing it reads,
and holds that the job runs it.

Run: uv run pytest scripts/validate/tests/test_node_units_case.py
"""

from validate import host_package_cases
from validate.validate_lib import case_groups, node_units_case
from validate.validate_lib.node_units_case import (
    WIFI_FILES,
    WIFI_OLD,
    nut_monitor_problems,
    wifi_copy_problems,
)

# The valid file installed, the two netplan refuses left the old one in place.
WIFI_RIGHT = {
    name: (0, rendered) if accepted else (1, WIFI_OLD)
    for name, (rendered, accepted) in WIFI_FILES.items()
}

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


def test_an_installer_that_sorts_every_wifi_file_is_clean():
    assert wifi_copy_problems(WIFI_RIGHT) == []


def test_an_installer_that_refuses_a_valid_file_is_flagged():
    aborted = {**WIFI_RIGHT, "valid": (1, WIFI_OLD)}
    assert wifi_copy_problems(aborted) == [
        "the valid file: the installer exited 1 and kept the old file"
    ]


def test_an_installer_that_installs_a_file_netplan_refuses_is_flagged():
    rendered = WIFI_FILES["short passphrase"][0]
    accepted = {**WIFI_RIGHT, "short passphrase": (0, rendered)}
    assert wifi_copy_problems(accepted) == [
        "the short passphrase file: the installer exited 0 and installed it"
    ]


def test_the_case_runs_the_shipped_files():
    assert node_units_case.NUT_DROPIN_SOURCE.is_file()
    assert node_units_case.WIFI_SCRIPT_SOURCE.is_file()


def test_the_host_packages_job_runs_the_case():
    runs = [run for _, run in case_groups.groups(host_package_cases.Cases)]
    assert node_units_case.run in runs
