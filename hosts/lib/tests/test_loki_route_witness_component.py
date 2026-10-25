"""The rendered `loki-route-witness` component: daniel-box's Loki read-route witness (H7.10c).

The script's behaviour is run against stubs in `ansible/tests/setup/test_loki_route_witness.py`.
These tests hold what the move added: the unit that runs it, the one credential it reads, the
timer and boot-rerun shape of the kuma-check form M6 left, and the legacy entries that retire
the Ansible-era timer on daniel-box. daniel-server keeps that timer, so the role's tasks skip
daniel-box through `initial_setup_packaged_hosts`.

Run: uv run pytest hosts/lib/tests/test_loki_route_witness_component.py
"""

import pytest
import yaml
from _secret_rotation_package import LIB, UNITS, render_fixture, text

from lib.estate import role_defaults
from lib.repo_paths import REPO

COMPONENT = REPO / "hosts" / "components" / "loki-route-witness" / "component.yml"
CREDENTIAL = "loki_route_witness_push_token"
CONTRACT_PARTS = (
    "Autonomous-role contract",
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
    # The credential is read at run time; no token and no env file is rendered in.
    assert "CREDENTIALS_DIRECTORY" in script
    assert "loki-route-kuma-push.env" not in script


def test_the_service_loads_the_credential_and_reruns_while_red(package):
    service = text(package, f"{UNITS}/homelab-loki-route-witness.service")
    assert f"LoadCredential={CREDENTIAL}:/run/credstore/{CREDENTIAL}\n" in service
    assert f"ExecStart={LIB}/loki-read-route-health\n" in service
    assert "Restart=on-failure\nRestartSec=15min\n" in service
    boot = text(package, f"{UNITS}/homelab-loki-route-witness-boot.service")
    assert "ConditionPathExists=/var/lib/kuma-check/loki-route-witness.down" in boot


def test_the_timer_fires_hourly_at_minute_23_and_the_links_are_enabled(package):
    timer = text(package, f"{UNITS}/homelab-loki-route-witness.timer")
    assert "OnCalendar=*-*-* *:23:00\n" in timer
    assert {
        "homelab-loki-route-witness.timer",
        "homelab-loki-route-witness-boot.service",
    } <= set(package.enable_links())
    assert package.credentials.count(CREDENTIAL) == 1


def test_the_package_retires_the_ansible_era_timer_cron_and_files(package):
    kept = {
        e.get("unit") or e.get("cron") or e.get("path")
        for e in package.legacy
        if "oki" in str(e)
    }
    assert kept >= {
        "kuma-check-loki-read-route.timer",
        "kuma-check-loki-read-route-boot.service",
        "Loki read-route witness",
        "/usr/local/bin/loki-read-route-health.sh",
        "/etc/homelab/loki-route-kuma-push.env",
    }


def test_the_role_skips_daniel_box_and_keeps_daniel_server():
    defaults = role_defaults(REPO / "ansible" / "roles" / "setup" / "initial_setup")
    assert defaults["initial_setup_packaged_hosts"]["loki_route_witness"] == [
        "daniel-box"
    ]
    spec = yaml.safe_load(COMPONENT.read_text())
    assert spec["credentials"] == [CREDENTIAL]


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
