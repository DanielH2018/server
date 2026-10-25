"""The rendered `infra-map` component: the infrastructure-map timer in daniel-box's package (H7.10a).

Until H7.36 `initial_setup` installed the map refresh as a cron. These tests hold what the move
carries: the unit's user, working directory and environment, which are what the cron's job line
set, the discarded stdout, the 15-minute timer with its enable link, and the legacy cron entry.

Run: uv run pytest hosts/lib/tests/test_infra_map_component.py
"""

import pytest
from _checkout_package import render_daniel_box

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "infra-map" / "component.yml"
UNITS = "/usr/lib/systemd/system"
CONTRACT_PARTS = (
    "Autonomous-role contract",
    "Scope / exclusions:",
    "Mode (explicit and reversible):",
    "Authoritative sources:",
    "Abort valves:",
    "Required evidence:",
    "Next-run review:",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(tmp_path_factory.mktemp("infra-map"), "infra-map")


def _text(package: host_mode.Package, name: str) -> str:
    return package.files[f"{UNITS}/{name}"][0].decode()


def test_the_service_runs_the_generator_as_sys_user_with_the_crons_environment(package):
    user = render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]
    unit = _text(package, "homelab-infra-map.service")
    assert f"User={user}\n" in unit
    assert f"WorkingDirectory=/home/{user}/server\n" in unit
    assert "Environment=PATH=/usr/local/bin:/usr/bin:/bin\n" in unit
    assert f"Environment=KUBECONFIG=/home/{user}/.kube/config\n" in unit
    assert "scripts/infra_map/gen_infra_map.py -o" in unit
    assert f"/home/{user}/.claude/artifacts/homelab-infra-map.html\n" in unit


def test_stdout_is_discarded_and_stderr_is_not(package):
    unit = _text(package, "homelab-infra-map.service")
    assert "StandardOutput=null\n" in unit
    assert "StandardError" not in unit


def test_the_service_reads_no_credential_and_does_not_sandbox_the_home(package):
    unit = _text(package, "homelab-infra-map.service")
    assert "LoadCredential" not in unit
    assert "ProtectHome" not in unit


def test_the_timer_fires_every_15_minutes_and_is_linked(package):
    assert "OnCalendar=*-*-* *:00,15,30,45:00\n" in _text(
        package, "homelab-infra-map.timer"
    )
    assert "homelab-infra-map.timer" in package.enable_links()


def test_the_package_retires_the_ansible_era_cron(package):
    assert {"cron": "Refresh homelab infrastructure map", "user": "ubuntu"} in [
        entry for entry in package.legacy
    ]


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
    reject = header.replace("Next-run review:", "")
    assert [part for part in CONTRACT_PARTS if part not in reject] == [
        "Next-run review:"
    ]
