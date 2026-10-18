#!/usr/bin/env python3
"""observability's LAN-address rules name the Service and address each role renders.

Each rule is its role's soak verify: it fires when kube-state-metrics reports no LoadBalancer
ingress at the pinned VIP on the role's LoadBalancer Service. A rule file is never Jinja, so the
namespace, the Service name and the address are literals in its selector. Move the VIP in
group_vars, or rename the Service, and the rule fires on every revision while the workload is
healthy. Drop the ingress family from the scrape and the rule reads the drop as a missing
address. Each literal is therefore held to the rendered Service, not to a role default, so a
template that stopped using the variable is caught too.

Run: uv run pytest ansible/tests/services/test_lan_address_rules_agree.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _k8s_render import rendered_docs
from lib import yaml_fast

_RULES = K8S_ROLES / "observability" / "files" / "rules" / "k8s-services.yaml"
_SELECTOR = re.compile(r"kube_service_status_load_balancer_ingress\{([^}]*)\}")
_MATCHER = re.compile(r'(\w+)="([^"]*)"')

# (alert name, the role whose LoadBalancer Service it watches)
RULES = [
    ("MosquittoLanAddressNotPinned", "mosquitto"),
    ("JellyfinLanAddressNotPinned", "jellyfin"),
    ("PiholeLanAddressNotPinned", "pihole"),
    ("TraefikIngressAddressNotPinned", "traefik"),
]


def rule(name: str) -> dict:
    groups = yaml_fast.safe_load(_RULES.read_text())["groups"]
    return next(r for g in groups for r in g["rules"] if r.get("alert") == name)


def selector_matchers(expr: str) -> dict[str, str]:
    """The equality matchers on the rule's one kube_service_status_load_balancer_ingress selector."""
    selectors = _SELECTOR.findall(expr)
    assert len(selectors) == 1, (
        f"the rule must read one ingress selector, found {selectors}"
    )
    return dict(_MATCHER.findall(selectors[0]))


def lan_services(role: str) -> list[dict]:
    """The role's rendered LoadBalancer Services."""
    return [
        doc
        for rendered_role, _tpl, doc in rendered_docs()
        if rendered_role == role
        and doc.get("kind") == "Service"
        and doc.get("spec", {}).get("type") == "LoadBalancer"
    ]


def test_selector_matchers_reads_each_equality_matcher():
    expr = 'absent(kube_service_status_load_balancer_ingress{a="1", ip="10.0.0.1"})'
    assert selector_matchers(expr) == {"a": "1", "ip": "10.0.0.1"}


@pytest.mark.parametrize(("name", "role"), RULES)
def test_the_rule_selects_the_service_and_address_the_role_renders(name, role):
    services = lan_services(role)
    assert len(services) == 1, (
        f"{role} must render one LoadBalancer Service: {services}"
    )
    meta = services[0]["metadata"]
    want = {
        "namespace": meta["namespace"],
        "service": meta["name"],
        "ip": str(meta["annotations"]["metallb.io/loadBalancerIPs"]),
    }
    got = selector_matchers(rule(name)["expr"])
    assert {k: got.get(k) for k in want} == want


@pytest.mark.parametrize(("name", "role"), RULES)
def test_the_rule_is_the_roles_verify_at_warning(name, role):
    labels = rule(name)["labels"]
    assert labels.get("service") == role
    assert labels.get("verify") == "true"
    assert labels.get("severity") == "warning"
    assert "shadow" not in labels
