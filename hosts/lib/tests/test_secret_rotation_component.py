"""The rendered `secret-rotation` component: the weekly rotation and the daily audit (H7.10c).

The scripts' behaviour is run against stubs in `ansible/tests/setup/`. These tests hold what the
move added: the units that run the scripts, the one credential both read, the enable links, the
legacy entries that retire the Ansible-era cron and timer, and the Autonomous-role contract.

Run: uv run pytest hosts/lib/tests/test_secret_rotation_component.py
"""

import pytest
from _secret_rotation_package import LIB, UNITS, render_fixture, text

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO

COMPONENT = REPO / "hosts" / "components" / "secret-rotation" / "component.yml"
CREDENTIAL = "secret_rotation_push_token"
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


def test_both_scripts_install_executable_and_carry_no_token(package):
    for name in ("secret-rotate", "secret-rotation-audit"):
        body = text(package, f"{LIB}/{name}")
        assert package.files[f"{LIB}/{name}"][1] == 0o755
        # The credential is read at run time from the unit's directory, never rendered in.
        assert "CREDENTIALS_DIRECTORY" in body
        assert "STUB" not in body


@pytest.mark.parametrize(
    ("unit", "script"),
    [
        ("homelab-secret-rotation", "secret-rotate"),
        ("homelab-secret-rotation-audit", "secret-rotation-audit"),
    ],
)
def test_each_service_runs_its_script_as_sys_user_with_the_credential(
    package, unit, script
):
    user = render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]
    service = text(package, f"{UNITS}/{unit}.service")
    assert f"User={user}\n" in service
    assert f"ExecStart={LIB}/{script}\n" in service
    assert f"LoadCredential={CREDENTIAL}:/run/credstore/{CREDENTIAL}\n" in service
    assert "Wants=network-online.target homelab-secrets.service" in service


def test_each_service_keeps_the_journal_tag_the_cron_logged_under(package):
    """`journalctl -t secret-rotate` and the Loki streams keyed on it replaced a `logger -t`."""
    rotation = text(package, f"{UNITS}/homelab-secret-rotation.service")
    audit = text(package, f"{UNITS}/homelab-secret-rotation-audit.service")
    assert "SyslogIdentifier=secret-rotate\n" in rotation
    assert "SyslogIdentifier=secret-rotation-audit\n" in audit


def test_the_audit_reruns_while_red_and_a_boot_unit_covers_a_reboot(package):
    service = text(package, f"{UNITS}/homelab-secret-rotation-audit.service")
    assert "Restart=on-failure\nRestartSec=30min\n" in service
    assert "secret-rotation-audit.down" in service
    boot = text(package, f"{UNITS}/homelab-secret-rotation-audit-boot.service")
    assert "ConditionPathExists=/var/lib/kuma-check/secret-rotation-audit.down" in boot
    assert "start --no-block homelab-secret-rotation-audit.service" in boot


def test_the_timers_keep_the_cron_and_kuma_check_slots(package):
    weekly = text(package, f"{UNITS}/homelab-secret-rotation.timer")
    assert "OnCalendar=Sun *-*-* 09:00:00\n" in weekly
    audit = text(package, f"{UNITS}/homelab-secret-rotation-audit.timer")
    assert "OnCalendar=*-*-* 08:30:00\n" in audit
    assert "Persistent=true\n" in audit


def test_the_component_links_both_timers_and_the_boot_unit(package):
    assert {
        "homelab-secret-rotation.timer",
        "homelab-secret-rotation-audit.timer",
        "homelab-secret-rotation-audit-boot.service",
    } <= set(package.enable_links())


def test_the_package_names_the_credential_once(package):
    assert package.credentials.count(CREDENTIAL) == 1


def test_the_package_retires_the_ansible_era_cron_timer_and_scripts(package):
    legacy = [e for e in package.legacy if "secret" in str(e)]
    assert {e.get("cron") or e.get("unit") or e.get("path") for e in legacy} >= {
        "Weekly secret rotation (auto tier)",
        "Daily secret rotation audit",
        "kuma-check-secret-rotation-audit.timer",
        "/usr/local/bin/secret-rotate.sh",
        "/usr/local/bin/secret-rotation-audit.sh",
    }


def test_the_component_header_carries_the_contract():
    header = "\n".join(
        line for line in COMPONENT.read_text().splitlines() if line.startswith("#")
    )
    assert [part for part in CONTRACT_PARTS if part not in header] == []
