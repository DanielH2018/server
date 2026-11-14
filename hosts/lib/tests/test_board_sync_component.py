"""The rendered `board-sync` component: the hourly Project board resync in daniel-box's package.

Run: uv run pytest hosts/lib/tests/test_board_sync_component.py
"""

import pytest
import yaml
from _checkout_package import render_daniel_box

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "board-sync" / "component.yml"
PACKAGE = REPO / "hosts" / "daniel-box" / "package.yml"
UNITS = "/usr/lib/systemd/system"
CONTRACT_PARTS = (
    "Autonomous-role contract",
    "Scope / exclusions:",
    "Mode (explicit and reversible):",
    "Authoritative sources:",
    "Abort valves:",
    "Required evidence",
    "Next-run review:",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(tmp_path_factory.mktemp("board-sync"), "board-sync")


def _text(package: host_mode.Package, name: str) -> str:
    return package.files[f"{UNITS}/{name}"][0].decode()


def test_the_service_runs_sync_board_as_sys_user_in_the_primary_checkout(package):
    user = render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]
    unit = _text(package, "homelab-board-sync.service")
    assert f"User={user}\n" in unit
    assert f"WorkingDirectory=/home/{user}/server\n" in unit
    assert (
        f"ExecStart=/home/{user}/.local/bin/uv run --quiet python "
        "scripts/dev/findings.py sync-board\n"
    ) in unit
    assert "--dry-run" not in unit


def test_a_failed_run_pages_and_the_service_loads_no_credential(package):
    unit = _text(package, "homelab-board-sync.service")
    assert "OnFailure=homelab-alert@%n.service\n" in unit
    assert "Credential" not in unit


def test_daniel_box_ships_the_component_beside_the_alert_it_pages_through():
    components = yaml.safe_load(PACKAGE.read_text())["components"]
    assert {"board-sync", "failure-alert"} <= set(components)


def test_the_timer_fires_hourly_and_is_linked(package):
    assert "OnCalendar=*-*-* *:47:00\n" in _text(package, "homelab-board-sync.timer")
    assert "homelab-board-sync.timer" in package.enable_links()


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
