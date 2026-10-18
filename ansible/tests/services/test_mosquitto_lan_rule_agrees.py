#!/usr/bin/env python3
"""observability's MosquittoLanAddressNotPinned names the Service and address mosquitto renders.

The rule is mosquitto's soak verify: it fires when kube-state-metrics reports no LoadBalancer
ingress at the pinned VIP on the mosquitto-lan Service. A rule file is never Jinja, so the
namespace, the Service name and the address are literals in its selector. Move the VIP in
group_vars, or rename the Service, and the rule fires on every revision while the broker is
healthy. Drop the ingress family from the scrape and the rule reads the drop as a missing
address. Each literal is therefore held to the rendered Service, not to a role default, so a
template that stopped using the variable is caught too.

Run: uv run pytest ansible/tests/services/test_mosquitto_lan_rule_agrees.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import rendered_docs
from lib import yaml_fast

_RULES = K8S_ROLES / "observability" / "files" / "rules" / "k8s-services.yaml"
_RULE = "MosquittoLanAddressNotPinned"
_SELECTOR = re.compile(r"kube_service_status_load_balancer_ingress\{([^}]*)\}")
_MATCHER = re.compile(r'(\w+)="([^"]*)"')


def rule() -> dict:
    groups = yaml_fast.safe_load(_RULES.read_text())["groups"]
    return next(r for g in groups for r in g["rules"] if r.get("alert") == _RULE)


def selector_matchers(expr: str) -> dict[str, str]:
    """The equality matchers on the rule's one kube_service_status_load_balancer_ingress selector."""
    selectors = _SELECTOR.findall(expr)
    assert len(selectors) == 1, (
        f"{_RULE} must read one ingress selector, found {selectors}"
    )
    return dict(_MATCHER.findall(selectors[0]))


def lan_services() -> list[dict]:
    """mosquitto's rendered LoadBalancer Services."""
    return [
        doc
        for role, _tpl, doc in rendered_docs()
        if role == "mosquitto"
        and doc.get("kind") == "Service"
        and doc.get("spec", {}).get("type") == "LoadBalancer"
    ]


def test_selector_matchers_reads_each_equality_matcher():
    expr = 'absent(kube_service_status_load_balancer_ingress{a="1", ip="10.0.0.1"})'
    assert selector_matchers(expr) == {"a": "1", "ip": "10.0.0.1"}


def test_the_rule_selects_the_service_and_address_mosquitto_renders():
    services = lan_services()
    assert len(services) == 1, (
        f"mosquitto must render one LoadBalancer Service: {services}"
    )
    meta = services[0]["metadata"]
    want = {
        "namespace": meta["namespace"],
        "service": meta["name"],
        "ip": str(meta["annotations"]["metallb.io/loadBalancerIPs"]),
    }
    got = selector_matchers(rule()["expr"])
    assert {k: got.get(k) for k in want} == want


def test_the_rule_is_mosquittos_verify_at_warning():
    labels = rule()["labels"]
    assert labels.get("service") == "mosquitto"
    assert labels.get("verify") == "true"
    assert labels.get("severity") == "warning"
    assert "shadow" not in labels
