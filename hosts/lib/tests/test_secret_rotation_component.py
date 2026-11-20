"""The rendered `secret-rotation` component: the daily audit (H7.10c).

The script's behaviour is run against stubs in `hosts/tests/`. These tests hold what the
move added: the units that run the script, which load no credential since M6 batch B2's
Cutover-2 dropped the Kuma push, the enable links, the legacy entries that retire the Ansible-era
crons and timer, and the Autonomous-role contract.

Run: uv run pytest hosts/lib/tests/test_secret_rotation_component.py
"""

import re
from datetime import date, timedelta

import pytest
from _k3s_schedule import calendar_fires
from _secret_rotation_package import LIB, UNITS, render_fixture, text

from _component_contract import component_contract
from _reboot_mute import MINUTES_PER_DAY, MINUTES_PER_WEEK, mute_minutes
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context

SUNDAY = date(2026, 11, 1)

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
    assert (
        "ConditionPathExists=/var/lib/homelab/check/secret-rotation-audit.down" in boot
    )
    assert "start --no-block homelab-secret-rotation-audit.service" in boot
    timer = text(package, f"{UNITS}/homelab-secret-rotation-audit.timer")
    assert "Persistent=true\n" in timer


def _audit_fires(package) -> list[int]:
    """The minutes of one week, from Sunday 00:00 UTC, at which the audit timer fires."""
    timer = text(package, f"{UNITS}/homelab-secret-rotation-audit.timer")
    [calendar] = re.findall(r"^OnCalendar=(.+)$", timer, re.M)
    fires = [
        day * MINUTES_PER_DAY + second // 60
        for day in range(7)
        for second in calendar_fires(calendar, SUNDAY + timedelta(days=day))
    ]
    assert fires, f"OnCalendar={calendar} fires on no day of the week"
    return fires


def test_the_audit_fires_outside_the_weekly_reboot_mute(package):
    """A verdict written inside Alertmanager's `weekly-reboot` mute pages only once it closes."""
    assert not mute_minutes().intersection(_audit_fires(package))


def test_the_audit_fires_again_inside_its_not_reporting_deadline(package):
    """`SecretRotationAuditNotReporting` pages on a verdict older than the script's DEADLINE_S."""
    script = text(package, f"{LIB}/secret-rotation-audit")
    [deadline] = re.findall(r"^DEADLINE_S=(\d+)$", script, re.M)
    fires = _audit_fires(package)
    gaps = [
        (later - earlier) % MINUTES_PER_WEEK or MINUTES_PER_WEEK
        for earlier, later in zip(fires, fires[1:] + fires[:1], strict=True)
    ]
    assert max(gaps) * 60 < int(deadline)


def test_the_component_links_the_audit_timer_and_the_boot_unit(package):
    assert {
        "homelab-secret-rotation-audit.timer",
        "homelab-secret-rotation-audit-boot.service",
    } <= set(package.enable_links())


def test_the_claude_md_carries_the_contract():
    contract = component_contract("secret-rotation")
    assert [part for part in CONTRACT_PARTS if part not in contract] == []
