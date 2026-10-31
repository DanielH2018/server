"""A dead Pi pages once: `PiHostDown` inhibits `PiPortDead`.

M6 batch B8's Cutover-b makes the `extra-probes` rules live. When the Pi goes down its node
exporter fails first (`PiHostDown`, `for: 2m`) and every published port closes with it
(`PiPortDead`, `for: 10m`, one alert per port). monitor-bridge's port arm sat inside a check that
`EXPORTER_DEPENDENT["node-pi"]` suppressed while the exporter was down, so the host paged alone.
The inhibit in `alertmanager-secret.yaml.j2` keeps that. It has no `equal` list, because
`PiHostDown` carries the exporter's `instance` and `PiPortDead` the port's, and the estate has one
Pi. `ExtraProbeFailed` for wg-easy is not held, as Kuma's WG Easy tile paged beside the ping tile.

This module evaluates the rendered `inhibit_rules` with the evaluator
`test_alertmanager_host_check_inhibit.py` uses, on label sets read from the shipped rules.

Run: uv run pytest ansible/tests/services/test_alertmanager_pi_inhibit.py
"""

from _helpers import ROLES
from lib import yaml_fast
from test_alertmanager_host_check_inhibit import _selects, inhibit_rules, inhibited

EXTRA_PROBES = ROLES / "k8s" / "observability" / "files" / "rules" / "extra-probes.yaml"
PI_ADDRESS = "192.0.2.10"


def rule_labels(alert: str) -> dict[str, str]:
    [group] = yaml_fast.safe_load(EXTRA_PROBES.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == alert]
    return rule["labels"]


def host_down() -> dict[str, str]:
    """The labels `PiHostDown` fires with: the node-pi series' labels plus the rule's."""
    return {
        "alertname": "PiHostDown",
        "job": "node-pi",
        "origin": "daniel-pi",
        "instance": f"{PI_ADDRESS}:9100",
        **rule_labels("PiHostDown"),
    }


def port_dead(service: str = "wg-easy", port: int = 51821) -> dict[str, str]:
    """The labels `PiPortDead` fires with: the probe series' labels plus the rule's."""
    return {
        "alertname": "PiPortDead",
        "job": "blackbox-pi-ports",
        "instance": f"{PI_ADDRESS}:{port}",
        "service": service,
        "module": "tcp_connect",
        **rule_labels("PiPortDead"),
    }


def port_inhibit() -> dict:
    [rule] = [r for r in inhibit_rules() if _selects(r, "source", host_down())]
    return rule


def test_a_dead_pi_holds_every_published_port_page():
    for service, port in (("wg-easy", 51821), ("alloy", 12345)):
        assert inhibited(port_dead(service, port), [host_down()])


def test_a_dead_port_still_pages_while_the_pi_host_is_up():
    # RED if the inhibit loses its source matcher: a lone detached container would be silent.
    assert not inhibited(port_dead(), [])


def test_the_pi_host_alert_holds_no_other_alert():
    extra = {
        "alertname": "ExtraProbeFailed",
        "job": "blackbox-extra",
        "instance": f"http://{PI_ADDRESS}:51821",
        "service": "wg-easy",
        "module": "http_2xx",
        "page_tier": "warning",
        "severity": "warning",
    }
    scrape = {
        "alertname": "ScrapeTargetDown",
        "job": "node-pi",
        "instance": f"{PI_ADDRESS}:9100",
        "service": "daniel-pi",
        "severity": "warning",
    }
    assert not inhibited(extra, [host_down()])
    assert not inhibited(scrape, [host_down()])


def test_the_rule_names_no_equal_label_the_two_alerts_disagree_on():
    # `instance` differs between PiHostDown (9100) and PiPortDead (the port), and `job` too, so
    # any `equal` entry naming either would never match.
    assert port_inhibit().get("equal", []) == []


def test_the_inhibit_source_and_target_are_both_live():
    # Inhibition runs before routing, so a live source over a shadow target (or the reverse)
    # would mute a page that is the only one, or leave two.
    assert "shadow" not in host_down()
    assert "shadow" not in port_dead()
