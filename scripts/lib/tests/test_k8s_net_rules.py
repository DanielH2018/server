#!/usr/bin/env python3
"""The two semantic rules on a rendered manifest that no schema can make.

An https IngressRoute with no `spec.tls` is not a TLS router at all, and a NetworkPolicy port
holding a Service's published number fences nothing. Both objects apply cleanly and then match
no traffic, which is why each needs a rule rather than a schema.

Split out of scripts/validate/tests/test_validate_k8s_manifests.py on 2026-09-04, with the
code it covers.

Run: uv run pytest scripts/lib/tests/test_k8s_net_rules.py
"""

from lib.k8s_net_rules import (
    https_route_without_tls,
    netpol_port_mismatches,
    service_port_translations,
    workload_container_ports,
)


# --- an https IngressRoute must declare spec.tls ---------------------------------------------


def _route(entrypoints, tls=None):
    doc = {
        "kind": "IngressRoute",
        "metadata": {"name": "thing"},
        "spec": {"entryPoints": entrypoints, "routes": []},
    }
    if tls is not None:
        doc["spec"]["tls"] = tls
    return doc


def test_an_https_route_without_tls_is_flagged():
    """Without `tls:` Traefik never treats the route as a TLS router, so it never matches."""
    assert "spec.tls" in https_route_without_tls(_route(["https"]))


def test_an_https_route_with_tls_is_clean():
    assert https_route_without_tls(_route(["https"], tls={})) is None


def test_an_https_route_with_tls_but_no_cert_resolver_is_clean():
    """An empty resolver means Traefik's own self-signed cert — legitimate, and not this rule."""
    assert (
        https_route_without_tls(_route(["https"], tls={"options": {"name": "modern"}}))
        is None
    )


def test_a_non_https_route_without_tls_is_clean():
    assert https_route_without_tls(_route(["web"])) is None


def test_a_non_ingressroute_is_clean():
    assert https_route_without_tls({"kind": "Service", "spec": {}}) is None


# --- a NetworkPolicy port is the container's, not the Service's -------------------------------

_WORKLOAD = {
    "kind": "Deployment",
    "spec": {
        "template": {
            "metadata": {"labels": {"app": "traefik"}},
            "spec": {"containers": [{"ports": [{"containerPort": 8000}]}]},
        }
    },
}
_TRANSLATING_SERVICE = {
    "kind": "Service",
    "spec": {"ports": [{"port": 80, "targetPort": 8000}]},
}


def _policy(port):
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": "fence"},
        "spec": {
            "podSelector": {"matchLabels": {"app": "traefik"}},
            "ingress": [{"ports": [{"port": port}]}],
        },
    }


def _mismatches(policy, docs):
    return netpol_port_mismatches(
        policy,
        workload_container_ports(docs),
        service_port_translations(docs),
    )


def test_a_policy_using_the_service_port_is_flagged():
    docs = [_WORKLOAD, _TRANSLATING_SERVICE]
    assert _mismatches(_policy(80), docs)


def test_a_policy_using_the_container_port_is_clean():
    docs = [_WORKLOAD, _TRANSLATING_SERVICE]
    assert _mismatches(_policy(8000), docs) == []


def test_an_undeclared_listener_port_is_not_flagged():
    """traefik's metrics endpoint answers on 8080 while declaring no containerPort.

    A containerPort declaration is informational, so "not declared" alone is not evidence of a
    mistake. Only a number that a Service publishes to a DIFFERENT target is.
    """
    docs = [_WORKLOAD, _TRANSLATING_SERVICE]
    assert _mismatches(_policy(8080), docs) == []


def test_a_service_mapping_one_to_one_creates_no_confusion():
    docs = [
        _WORKLOAD,
        {"kind": "Service", "spec": {"ports": [{"port": 9000, "targetPort": 9000}]}},
    ]
    assert _mismatches(_policy(9000), docs) == []


def test_a_policy_selecting_no_workload_is_clean():
    """No matching workload is no evidence, and must not read as a finding."""
    policy = _policy(80)
    policy["spec"]["podSelector"] = {"matchLabels": {"app": "nothing"}}
    assert _mismatches(policy, [_WORKLOAD, _TRANSLATING_SERVICE]) == []


def test_an_empty_pod_selector_is_clean():
    """A bare `podSelector: {}` selects every pod in the namespace — no single port to check."""
    policy = _policy(80)
    policy["spec"]["podSelector"] = {}
    assert _mismatches(policy, [_WORKLOAD, _TRANSLATING_SERVICE]) == []
