"""The cluster-node package container case's verdict and seed, without a container.

The case itself runs only in CI's `host_packages` job, once per Ubuntu release.

Run: uv run pytest scripts/validate/tests/host_cases/test_node_package_case.py
"""

from types import SimpleNamespace

from renderer.renderer_lib import host as host_mode
from validate import host_package_cases
from validate.validate_lib import case_groups
from validate.validate_lib.host_container import CaseFailed
from validate.validate_lib.node_package_case import (
    NODES,
    STUBBED_ONESHOTS,
    STUBBED_UNITS,
    TIMER_DROPIN,
    _install,
    run as run_node_case,
    case_dropins,
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


def test_every_timer_a_node_ships_is_held_off():
    """A timer left out would fire a networked service at whatever minute the case runs."""
    for package in map(host_mode.render_host, NODES):
        dropins = case_dropins(package)
        timers = {
            unit
            for unit in (*package.units(), *package.enable_links())
            if unit.endswith(".timer")
        }
        assert {"homelab-claude-guardrail-ack.timer", "homelab-updater.timer"} <= timers
        assert {t for t in timers if dropins.get(t) != TIMER_DROPIN} == set()


def test_the_host_packages_job_runs_the_case_once_per_node_in_a_group_of_its_own():
    booted = []
    cases = SimpleNamespace(
        boot=lambda label, hostname: booted.append((label, hostname)),
        case_seconds={},
    )
    names = []
    for name, run in case_groups.groups(host_package_cases.Cases):
        if name.startswith("node_package_case"):
            names.append(name)
            run(cases)

    assert {"names": names, "booted": booted} == {
        "names": [f"node_package_case-{host}" for host in NODES],
        "booted": [(f"node-{host}", host) for host in NODES],
    }


def test_install_records_each_steps_seconds_for_the_done_line():
    c = SimpleNamespace(
        name="node-daniel-box", ok=lambda command: "", sh=lambda command: None
    )
    cases = SimpleNamespace(case_seconds={})
    package = SimpleNamespace(units=lambda: [], enable_links=lambda: [])

    _install(cases, c, "daniel-box", lambda *_: (package, "/srv/debs/a.deb"))

    assert list(cases.case_seconds) == [
        "mask gettys",
        "seal and build",
        "download",
        "seed",
        "apt install",
        "reconcile",
        "settle",
        "check",
    ]


def test_run_times_the_boot_and_the_removal_the_install_does_not():
    def refuse(command):
        raise CaseFailed("refused")

    c = SimpleNamespace(name="node-daniel-box", ok=refuse, remove=lambda: None)
    cases = SimpleNamespace(
        boot=lambda label, hostname: c, case_seconds={}, passed=[], failed=[]
    )

    run_node_case(cases, "daniel-box")

    assert set(cases.case_seconds) == {"boot", "mask gettys", "remove"}
