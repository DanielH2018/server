"""The cluster-node package container case's verdict and seed, without a container.

The case itself runs only in CI's `host_packages` job, once per Ubuntu release.

Run: uv run pytest scripts/validate/tests/test_node_package_case.py
"""

from renderer.renderer_lib import host as host_mode
from validate import host_package_cases
from validate.validate_lib import case_groups, node_package_case
from validate.validate_lib.node_package_case import (
    NODES,
    STUBBED_ONESHOTS,
    STUBBED_UNITS,
    unit_problems,
)


RUNNING = "Id=nut-monitor.service\nActiveState=active\nSubState=running\nNRestarts=0\n"
TIMER = "Id=homelab-updater.timer\nActiveState=active\nSubState=waiting\n"


def test_running_and_waiting_units_are_clean():
    assert unit_problems(f"{RUNNING}\n{TIMER}") == {}


def test_a_failed_unit_is_flagged():
    failed = "Id=homelab-reconcile.service\nActiveState=failed\nSubState=failed\nNRestarts=0\n"
    assert unit_problems(f"{RUNNING}\n{failed}") == {
        "homelab-reconcile.service": "failed"
    }


def test_a_service_in_a_restart_loop_is_flagged():
    looping = RUNNING.replace("SubState=running", "SubState=auto-restart").replace(
        "NRestarts=0", "NRestarts=3"
    )
    assert unit_problems(f"{looping}\n{TIMER}") == {
        "nut-monitor.service": "restarted itself 3 times"
    }


def _shipped_units() -> dict[str, str]:
    """Each unit file a node package ships, by name, mapped to its text."""
    shipped = {}
    for package in map(host_mode.render_host, NODES):
        units = set(package.units())
        for path, (content, _) in package.files.items():
            if path.rsplit("/", 1)[1] in units:
                shipped[path.rsplit("/", 1)[1]] = content.decode()
    return shipped


def test_every_stubbed_unit_is_one_a_node_package_ships():
    shipped = _shipped_units()
    assert {*STUBBED_UNITS, *STUBBED_ONESHOTS} - set(shipped) == set()


def test_each_stub_matches_its_units_type():
    """A `sleep` stub on a oneshot would hold its start until the step's timeout."""
    shipped = _shipped_units()
    oneshots = {u for u, text in shipped.items() if "\nType=oneshot" in text}
    assert set(STUBBED_ONESHOTS) - oneshots == set()
    assert set(STUBBED_UNITS) & oneshots == set()


def test_the_host_packages_job_runs_the_case():
    runs = [run for _, run in case_groups.groups(host_package_cases.Cases)]
    assert node_package_case.run in runs
