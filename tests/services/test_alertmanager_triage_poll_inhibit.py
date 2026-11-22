"""A triage poll that fails every tick pages once: its hook-paged unit failure holds the staleness alert.

`homelab-claude-triage-poll.service` pages Discord through its own OnFailure hook, and the
systemd-units check labels its `SystemdUnitsFailed` with `paged_by="homelab-alert"`. The inhibit in
`alertmanager-secret.yaml.j2` lets that alert hold `ClaudeTriagePollNotReporting` on the same
origin. A stopped timer fails no unit, so the staleness alert still pages it.

This module evaluates the rendered `inhibit_rules` with the evaluator
`test_alertmanager_host_check_inhibit.py` uses, on label sets read from the shipped rules.

Run: uv run pytest tests/services/test_alertmanager_triage_poll_inhibit.py
"""

from test_alertmanager_host_check_inhibit import host_rules, inhibited

NODE = {"job": "node", "instance": "daniel-box", "origin": "daniel-box"}


def rule_labels(alert: str) -> dict[str, str]:
    [labels] = {
        tuple(r["labels"].items())
        for r in host_rules()
        if r["alert"] == alert and r["labels"]["service"] == "daniel-box"
    }
    return dict(labels)


def poll_stale() -> dict[str, str]:
    return {
        "alertname": "ClaudeTriagePollNotReporting",
        "check": "claude-triage-poll",
        **NODE,
        **rule_labels("ClaudeTriagePollNotReporting"),
    }


def unit_failed(
    unit: str = "homelab-claude-triage-poll.service", **extra: str
) -> dict[str, str]:
    return {
        "alertname": "SystemdUnitsFailed",
        "check": "systemd-units",
        "unit": unit,
        **NODE,
        **rule_labels("SystemdUnitsFailed"),
        **extra,
    }


def test_a_poll_failure_its_hook_paged_holds_the_staleness_page():
    assert inhibited(poll_stale(), [unit_failed(paged_by="homelab-alert")])


def test_only_a_hook_paged_failure_of_the_poll_unit_holds_it():
    assert not inhibited(poll_stale(), [unit_failed()])
    other = unit_failed("homelab-claude-memory-sync.service", paged_by="homelab-alert")
    assert not inhibited(poll_stale(), [other])
