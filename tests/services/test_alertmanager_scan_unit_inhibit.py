"""A failed weekly scan pages once: its own `*Failed` alert holds `SystemdUnitsFailed` for its unit.

A failed AIDE or rkhunter run exits non-zero, so its unit lands in the failed state and
`SystemdUnitsFailed` fires for it beside `AideCheckFailed` or `RkhunterScanFailed`. The inhibits
in `alertmanager-secret.yaml.j2` hold that copy on the same `origin`. `SystemdUnitsFailed` is one
alert per failed unit, so every other failed unit on the host keeps paging.

This module evaluates the rendered `inhibit_rules` with the evaluator
`test_alertmanager_host_check_inhibit.py` uses, on label sets read from the shipped rules.

Run: uv run pytest tests/services/test_alertmanager_scan_unit_inhibit.py
"""

import pytest
from _helpers import K8S_ROLES
from lib import yaml_fast
from test_alertmanager_host_check_inhibit import inhibited

HOST_RULES = K8S_ROLES / "observability" / "files" / "rules" / "host-checks.yaml"
SCANS = {
    "AideCheckFailed": ("aide-check", "homelab-aide-check.service"),
    "RkhunterScanFailed": ("rkhunter-check", "homelab-rkhunter-check.service"),
}


def rule_labels(alert: str, host: str) -> dict[str, str]:
    [group] = yaml_fast.safe_load(HOST_RULES.read_text())["groups"]
    [rule] = [
        r
        for r in group["rules"]
        if r["alert"] == alert and r["labels"]["service"] == host
    ]
    return rule["labels"]


def scan_failed(alert: str, host: str) -> dict[str, str]:
    """The labels a scan's Failed alert fires with: the textfile series' plus the rule's."""
    check, _ = SCANS[alert]
    return {
        "alertname": alert,
        "check": check,
        "job": "node-pi" if host == "daniel-pi" else "node",
        "instance": host,
        "origin": host,
        **rule_labels(alert, host),
    }


def unit_failed(unit: str, host: str) -> dict[str, str]:
    """The labels `SystemdUnitsFailed` fires with for one failed unit on `host`."""
    return {
        "alertname": "SystemdUnitsFailed",
        "check": "systemd-units",
        "job": "node-pi" if host == "daniel-pi" else "node",
        "instance": host,
        "origin": host,
        "unit": unit,
        **rule_labels("SystemdUnitsFailed", host),
    }


SCAN_HOSTS = [
    (alert, host)
    for alert in SCANS
    for host in ("daniel-box", "daniel-server", "daniel-pi")
    if not (alert == "AideCheckFailed" and host == "daniel-pi")
]


@pytest.mark.parametrize(("alert", "host"), SCAN_HOSTS)
def test_a_failed_scan_holds_its_own_unit_on_the_same_host(alert, host):
    _, unit = SCANS[alert]
    assert inhibited(unit_failed(unit, host), [scan_failed(alert, host)])


def test_another_failed_unit_on_the_host_still_pages():
    source = scan_failed("RkhunterScanFailed", "daniel-box")
    assert not inhibited(unit_failed("nut-monitor.service", "daniel-box"), [source])


def test_the_scan_unit_on_another_host_still_pages():
    source = scan_failed("RkhunterScanFailed", "daniel-box")
    target = unit_failed("homelab-rkhunter-check.service", "daniel-server")
    assert not inhibited(target, [source])


def test_one_scan_does_not_hold_the_other_scans_unit():
    source = scan_failed("AideCheckFailed", "daniel-server")
    target = unit_failed("homelab-rkhunter-check.service", "daniel-server")
    assert not inhibited(target, [source])
