"""The rendered `secret-rotation` component: the daily audit (H7.10c).

The script's behaviour is run against stubs in `hosts/tests/`. These tests hold what the
move added: the units that run the script, which load no credential since M6 batch B2's
Cutover-2 dropped the Kuma push, the enable links, the legacy entries that retire the Ansible-era
crons and timer, and the Autonomous-role contract.

Run: uv run pytest hosts/lib/tests/test_secret_rotation_component.py
"""

import pytest
from _secret_rotation_package import LIB, UNITS, render_fixture, text

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO

COMPONENT = REPO / "hosts" / "components" / "secret-rotation" / "component.yml"
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
    return render_fixture(tmp_path_factory.mktemp("daniel-box"), ("secret-rotation",))


def test_the_script_installs_executable_and_reads_no_credential(package):
    body = text(package, f"{LIB}/secret-rotation-audit")
    assert package.files[f"{LIB}/secret-rotation-audit"][1] == 0o755
    assert "CREDENTIALS_DIRECTORY" not in body
    assert "STUB" not in body


def test_the_service_runs_its_script_as_sys_user_with_no_credential(package):
    user = render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]
    service = text(package, f"{UNITS}/homelab-secret-rotation-audit.service")
    assert f"User={user}\n" in service
    assert f"ExecStart={LIB}/secret-rotation-audit\n" in service
    assert "LoadCredential=" not in service
    assert "Wants=network-online.target\n" in service


def test_the_service_keeps_the_journal_tag_the_cron_logged_under(package):
    """The Loki streams keyed on `secret-rotation-audit` replaced a `logger -t`."""
    audit = text(package, f"{UNITS}/homelab-secret-rotation-audit.service")
    assert "SyslogIdentifier=secret-rotation-audit\n" in audit


def test_the_audit_reruns_while_red_and_a_boot_unit_covers_a_reboot(package):
    service = text(package, f"{UNITS}/homelab-secret-rotation-audit.service")
    assert "Restart=on-failure\nRestartSec=30min\n" in service
    assert "secret-rotation-audit.down" in service
    boot = text(package, f"{UNITS}/homelab-secret-rotation-audit-boot.service")
    assert "ConditionPathExists=/var/lib/kuma-check/secret-rotation-audit.down" in boot
    assert "start --no-block homelab-secret-rotation-audit.service" in boot


def test_the_timer_keeps_the_kuma_check_slot(package):
    audit = text(package, f"{UNITS}/homelab-secret-rotation-audit.timer")
    assert "OnCalendar=*-*-* 08:30:00\n" in audit
    assert "Persistent=true\n" in audit


def test_the_component_links_the_audit_timer_and_the_boot_unit(package):
    assert {
        "homelab-secret-rotation-audit.timer",
        "homelab-secret-rotation-audit-boot.service",
    } <= set(package.enable_links())


def test_the_package_ships_no_weekly_rotation(package):
    """The `auto` tier is retired, so no unit or script rotates a secret unattended."""
    assert f"{UNITS}/homelab-secret-rotation.timer" not in package.files
    assert f"{UNITS}/homelab-secret-rotation.service" not in package.files
    assert f"{LIB}/secret-rotate" not in package.files
    assert "homelab-secret-rotation.timer" not in set(package.enable_links())


def test_the_package_retires_the_ansible_era_cron_timer_and_scripts(package):
    legacy = [e for e in package.legacy if "secret" in str(e)]
    assert {e.get("cron") or e.get("unit") or e.get("path") for e in legacy} >= {
        "Weekly secret rotation (auto tier)",
        "Daily secret rotation audit",
        "/usr/local/bin/secret-rotate.sh",
        "/usr/local/bin/secret-rotation-audit.sh",
    }


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
