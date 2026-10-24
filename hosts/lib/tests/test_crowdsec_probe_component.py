"""The rendered `crowdsec-probe` component: crowdsec's edge probe timer in the Pi package (H5.14).

D5's first crowdsec conversion installed the timer through `setup/k8s_host_files`. These tests
hold what the move added: the script and unit paths, the timer's enable link, the legacy entries
that retire the Ansible-era units, and the one `User=` every unit writing `/var/log/pi-health/`
declares. The script's behaviour is run against a stub `curl` in
`ansible/tests/services/test_crowdsec_edge_probe_contract.py`.

Run: uv run pytest hosts/lib/tests/test_crowdsec_probe_component.py
"""

import re

import pytest

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "crowdsec-probe" / "component.yml"
UNITS = "/usr/lib/systemd/system"
SCRIPT = "/usr/lib/homelab/crowdsec-edge-probe"
# Every unit that declares LogsDirectory=pi-health in the Pi package: the three heartbeats of
# pi-heartbeats and this component's probe. The census must find all four.
LOGS_DIRECTORY_UNITS = frozenset(
    f"homelab-{name}.service"
    for name in ("pi-sd-health", "pi-gz-integrity", "pi-recovery", "crowdsec-probe")
)
CONTRACT_PARTS = (
    "Autonomous-role contract",
    "Scope / exclusions:",
    "Mode (explicit and reversible):",
    "Authoritative sources:",
    "Abort valves:",
    "Required evidence, every cycle:",
    "Next-run review:",
)


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def logs_directory_problems(units: dict[str, str]) -> list[str]:
    """Why the units declaring `LogsDirectory=pi-health` do not share one `User=`, [] when they do."""
    users = {
        name: re.findall(r"^User=(.*)$", text, re.MULTILINE)
        for name, text in units.items()
        if re.search(r"^LogsDirectory=pi-health$", text, re.MULTILINE)
    }
    problems = [
        f"{name} declares no User=" for name, found in users.items() if not found
    ]
    if len({tuple(found) for found in users.values()}) > 1:
        problems.append(f"the units name different users: {sorted(users.items())}")
    return problems


def _pi_health_units(package: host_mode.Package) -> dict[str, str]:
    return {
        dst.rsplit("/", 1)[1]: _text(package, dst)
        for dst in package.files
        if dst.startswith(UNITS) and dst.endswith(".service")
    }


def test_the_component_ships_the_script_and_links_the_timer(package):
    assert package.files[SCRIPT][1] == 0o755
    assert {
        f"{UNITS}/homelab-crowdsec-probe.{kind}" for kind in ("service", "timer")
    } <= set(package.files)
    assert "homelab-crowdsec-probe.timer" in package.enable_links()


def test_the_service_runs_the_script_as_sys_user_and_reads_no_credential(package):
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    unit = _text(package, f"{UNITS}/homelab-crowdsec-probe.service")
    assert f"User={user}\n" in unit
    assert f"ExecStart={SCRIPT} jellyfin.local." in unit
    assert "LoadCredential" not in unit


def test_the_timer_fires_every_minute_and_names_no_other_unit(package):
    timer = _text(package, f"{UNITS}/homelab-crowdsec-probe.timer")
    assert "OnUnitActiveSec=1min\n" in timer
    assert "Unit=" not in timer


def test_the_package_retires_the_ansible_era_units_and_script(package):
    assert [
        entry.get("unit") or entry.get("path")
        for entry in package.legacy
        if "crowdsec" in str(entry)
    ] == [
        "crowdsec-edge-probe.timer",
        "crowdsec-edge-probe.service",
        "/usr/local/bin/crowdsec-edge-probe",
    ]


def test_every_unit_writing_pi_health_names_the_same_user(package):
    units = _pi_health_units(package)
    declaring = {
        name for name, text in units.items() if "LogsDirectory=pi-health\n" in text
    }
    assert declaring == LOGS_DIRECTORY_UNITS
    assert logs_directory_problems(units) == []


def test_a_probe_unit_with_no_user_is_flagged(package):
    units = _pi_health_units(package)
    key = "homelab-crowdsec-probe.service"
    units[key] = re.sub(r"^User=.*\n", "", units[key], flags=re.MULTILINE)
    assert f"{key} declares no User=" in logs_directory_problems(units)


def test_a_probe_unit_running_as_another_user_is_flagged(package):
    units = _pi_health_units(package)
    key = "homelab-crowdsec-probe.service"
    units[key] = re.sub(r"^User=.*$", "User=root", units[key], flags=re.MULTILINE)
    (problem,) = logs_directory_problems(units)
    assert problem.startswith("the units name different users")


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
