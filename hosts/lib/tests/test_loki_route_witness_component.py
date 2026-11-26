"""The rendered `loki-route-witness` component: each cluster node's Loki read-route witness.

The script's behaviour is run against stubs in `hosts/tests/repo_crons/test_loki_route_witness.py`.
These tests hold what the move added: the unit that runs it, which loads no credential since
M6 batch B2's Cutover-2 dropped the Kuma push, and the timer and boot-rerun shape of the kuma-check form M6 left.
Both cluster nodes list the component, and the role keeps no witness task.

Run: uv run pytest hosts/lib/tests/test_loki_route_witness_component.py
"""

import pytest
import yaml
from _secret_rotation_package import LIB, UNITS, render_fixture, text

from _component_contract import component_contract
from lib.repo_paths import REPO

COMPONENT = REPO / "hosts" / "components" / "loki-route-witness" / "component.yml"
# The Kuma push token B2's token PR retired.
RETIRED = "loki_route_witness_push_token"
CONTRACT_PARTS = (
    "Scope / exclusions:",
    "Mode (explicit and reversible):",
    "Authoritative sources:",
    "Abort valves:",
    "Required evidence, every run:",
    "Next-run review:",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return render_fixture(
        tmp_path_factory.mktemp("daniel-box"), ("loki-route-witness",)
    )


def test_the_script_and_its_reader_install_and_the_script_calls_the_packaged_reader(
    package,
):
    script = text(package, f"{LIB}/loki-read-route-health")
    assert package.files[f"{LIB}/loki-read-route-health"][1] == 0o755
    assert f"{LIB}/loki_route_health.py" in script
    assert f"{LIB}/loki_route_health.py" in package.files
    assert "CREDENTIALS_DIRECTORY" not in script


def test_the_service_loads_no_credential_and_reruns_while_red(package):
    service = text(package, f"{UNITS}/homelab-loki-route-witness.service")
    assert "LoadCredential=" not in service
    assert f"ExecStart={LIB}/loki-read-route-health\n" in service
    assert "Restart=on-failure\nRestartSec=15min\n" in service
    boot = text(package, f"{UNITS}/homelab-loki-route-witness-boot.service")
    assert "ConditionPathExists=/var/lib/homelab/check/loki-route-witness.down" in boot


def test_the_timer_fires_hourly_at_minute_23_and_the_links_are_enabled(package):
    timer = text(package, f"{UNITS}/homelab-loki-route-witness.timer")
    assert "OnCalendar=*-*-* *:23:00\n" in timer
    assert {
        "homelab-loki-route-witness.timer",
        "homelab-loki-route-witness-boot.service",
    } <= set(package.enable_links())
    assert RETIRED not in package.credentials


def test_both_cluster_nodes_witness_and_the_pi_does_not():
    # One host cannot see a route dead only from the other, and daniel-pi is not in its ClientIP
    # set.
    listing = {
        host: "loki-route-witness"
        in yaml.safe_load((REPO / "hosts" / host / "package.yml").read_text())[
            "components"
        ]
        for host in ("daniel-box", "daniel-server", "daniel-pi")
    }
    assert listing == {"daniel-box": True, "daniel-server": True, "daniel-pi": False}
    assert "credentials" not in yaml.safe_load(COMPONENT.read_text())


def test_the_claude_md_carries_the_contract():
    contract = component_contract("loki-route-witness")
    assert [part for part in CONTRACT_PARTS if part not in contract] == []
