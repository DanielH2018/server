#!/usr/bin/env python3
"""A homepage widget dialling a fenced ClusterIP renders a red tile, and nothing else notices.

Homepage's widgets address their targets pod-to-pod (`http://<svc>.<ns>.svc.cluster.local:<port>`)
because most of those targets sit behind Authelia, which 401s a session-less widget. Pod-to-pod is
exactly what the namespace ingress baseline denies, so every one of those widgets depends on the
target's own NetworkPolicy naming `app: homepage`. Fenced out, homepage stays 1/1, `probe.py
health homepage` exits 0, and the tile shows a widget-proxy error only a human looking at the
dashboard would see.

The census walks services.yaml.j2 for in-namespace widget URLs, resolves each Service to the pod
label it selects (they diverge: the `scrutiny` Service selects `app: scrutiny-web`), and checks the
rendered NetworkPolicies for a rule admitting homepage on that port.

Paired, per the repo's red-proof rule, and non-vacuous: an empty census would pass an `all()`.

Run: uv run pytest ansible/tests/services/test_homepage_widget_netpol_edges.py
"""

import re
import sys as _sys

from _helpers import ANSIBLE as _ANSIBLE

_sys.path.insert(0, str(_ANSIBLE / "tests"))

from _k8s_render import rendered_docs

SERVICES_TEMPLATE = (
    _ANSIBLE / "roles" / "k8s" / "homepage" / "templates" / "services.yaml.j2"
)

# Widget targets this suite knows are in place. Named rather than counted: the census below is a
# regex over one file, and it returns an empty set the moment those URLs are reshaped — after
# which every `all()` in this module passes while checking nothing.
KNOWN_TARGETS = frozenset({("scrutiny", 8080), ("sonarr", 8989), ("radarr", 7878)})

WIDGET_URL = re.compile(
    r"http://([a-z0-9-]+)\.\{\{\s*k8s_namespace\s*\}\}\.svc\.cluster\.local:(\d+)"
)


def widget_targets(template_text: str) -> set[tuple[str, int]]:
    """(Service name, port) for every widget URL addressing this namespace's ClusterIPs."""
    return {(m.group(1), int(m.group(2))) for m in WIDGET_URL.finditer(template_text)}


def unfenced_targets(targets, services, policies) -> set[tuple[str, int]]:
    """Targets whose pod is selected by a policy that does NOT admit homepage on that port.

    `services` maps Service name -> the `app` label it selects. `policies` is a list of
    (selected app label, admitted app labels, admitted ports).
    """
    missing = set()
    for name, port in sorted(targets):
        app = services.get(name)
        if app is None:
            missing.add((name, port))
            continue
        selecting = [p for p in policies if p[0] == app]
        if not selecting:
            # No policy selects it beyond the namespace baseline, which admits traefik and
            # prometheus only — so homepage cannot reach it either.
            missing.add((name, port))
            continue
        if not any(
            "homepage" in admitted and port in ports for _, admitted, ports in selecting
        ):
            missing.add((name, port))
    return missing


def _live_services_and_policies():
    services: dict[str, str] = {}
    policies: list[tuple[str, set[str], set[int]]] = []
    for _role, _template, doc in rendered_docs():
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "Service":
            app = (doc.get("spec") or {}).get("selector", {}).get("app")
            if app:
                services[doc["metadata"]["name"]] = app
        elif doc.get("kind") == "NetworkPolicy":
            spec = doc.get("spec") or {}
            selected = (spec.get("podSelector") or {}).get("matchLabels", {}).get("app")
            if not selected:
                continue
            for rule in spec.get("ingress") or []:
                admitted: set[str] = set()
                for peer in rule.get("from") or []:
                    sel = peer.get("podSelector") or {}
                    if app := sel.get("matchLabels", {}).get("app"):
                        admitted.add(app)
                    # The *arr policies come from the shared `arr_networkpolicy` macro, which
                    # writes one matchExpressions `In` list rather than a peer per caller.
                    # Reading only matchLabels here would report all three as fenced out.
                    for expr in sel.get("matchExpressions") or []:
                        if expr.get("key") == "app" and expr.get("operator") == "In":
                            admitted.update(expr.get("values") or [])
                ports = {
                    p["port"]
                    for p in rule.get("ports") or []
                    if isinstance(p.get("port"), int)
                }
                policies.append((selected, admitted, ports))
    return services, policies


def test_every_in_namespace_widget_target_admits_homepage():
    """The accepting half, against the real tree."""
    services, policies = _live_services_and_policies()
    targets = widget_targets(SERVICES_TEMPLATE.read_text())
    assert unfenced_targets(targets, services, policies) == set()


def test_the_census_still_finds_the_targets_it_is_meant_to_cover():
    """Non-vacuity. The test above passes on an empty census."""
    assert KNOWN_TARGETS <= widget_targets(SERVICES_TEMPLATE.read_text())


def test_a_target_whose_policy_omits_homepage_is_flagged():
    """The rejecting half. A rule that flagged nothing would pass both tests above."""
    services = {"scrutiny": "scrutiny-web"}
    policies = [("scrutiny-web", {"monitor-bridge"}, {8080})]
    assert unfenced_targets({("scrutiny", 8080)}, services, policies) == {
        ("scrutiny", 8080)
    }


def test_a_target_admitted_on_the_wrong_port_is_flagged():
    """Admitting homepage is not enough — kube-router matches the POD port, not the Service port."""
    services = {"scrutiny": "scrutiny-web"}
    policies = [("scrutiny-web", {"monitor-bridge", "homepage"}, {9090})]
    assert unfenced_targets({("scrutiny", 8080)}, services, policies) == {
        ("scrutiny", 8080)
    }


def test_a_target_admitted_on_the_right_port_is_clean():
    services = {"scrutiny": "scrutiny-web"}
    policies = [("scrutiny-web", {"monitor-bridge", "homepage"}, {8080})]
    assert unfenced_targets({("scrutiny", 8080)}, services, policies) == set()
