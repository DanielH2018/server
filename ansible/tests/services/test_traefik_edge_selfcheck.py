"""Traefik's startupProbe must land on a router that the CrowdSec plugin failure kills.

#1322: Traefik's boot-time download of the CrowdSec bouncer plugin timed out, the `crowdsec`
Middleware resolved to "invalid middleware type", every router on the https entrypoint was
rejected, and the pod sat 3/3 Ready serving 404 to the whole fleet for 3.5 hours. The fix is a
startupProbe (deployment.yaml.j2) aimed at a router of Traefik's own
(edge-selfcheck-ingressroute.yaml.j2) that is rejected by that same failure.

The probe only detects anything because of a coupling that nothing in either file states: the
self-check router sits on an entrypoint whose static-config middleware chain names the crowdsec
Middleware. Move the router to another entrypoint, drop crowdsec from that entrypoint's chain,
or repoint the probe at a different path, and the probe still passes on a healthy edge while
detecting nothing — green, and checking nothing. That is the vacuity failure the repo's
"a check that finds its own subject by pattern" rule is about, so the assertions below name the
members they must find rather than counting.

Checked on the RENDERED manifests, not the templates' text — the indirection trap in
`textual-guard-checks-break-on-indirection`, and the same reason as the sibling
test_traefik_watched_namespaces.py.
"""

import sys
from typing import Any

import pytest
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from lib import yaml_fast  # noqa: E402

from validate.k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)

_ROLE = "traefik"
# daniel-box runs the bouncer plugin; daniel-stage sets traefik_k8s_manage_crowdsec false and
# so has neither the failure mode nor the probe.
_HOST = "daniel-box"
_HOST_WITHOUT_CROWDSEC = "daniel-stage"


def _context(host: str) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    entry = next(c for c in base["containers_list"] if c["name"] == _ROLE)
    # Role defaults FIRST: Ansible ranks host_vars above them. Same ordering as the sibling.
    return {**role_defaults(_ROLE, base), **base, "container_item": entry}


def _render(host: str, template: str) -> str:
    ctx = _context(host)
    env = make_env([K8S_ROLES / _ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, template, ctx)
    assert rendered is not None, (
        f"{_ROLE}/{template} failed to render for {host}: {err}"
    )
    return rendered


def _traefik_container(host: str) -> dict:
    doc = yaml_fast.safe_load(_render(host, "deployment.yaml.j2"))
    containers = doc["spec"]["template"]["spec"]["containers"]
    return next(c for c in containers if c["name"] == "traefik")


def _static_config(host: str) -> dict:
    """The Traefik config itself, not the ConfigMap wrapping it.

    static-config.yaml.j2's data value is a block scalar, so the config is a STRING at the
    manifest level and has to be parsed a second time.
    """
    doc = yaml_fast.safe_load(_render(host, "static-config.yaml.j2"))
    return yaml_fast.safe_load(doc["data"]["traefik.yml"])


def _selfcheck_route(host: str) -> dict:
    doc = yaml_fast.safe_load(_render(host, "edge-selfcheck-ingressroute.yaml.j2"))
    assert doc["kind"] == "IngressRoute", doc["kind"]
    return doc


def _crowdsec_middleware_ref(host: str) -> str:
    """The name the entrypoint chains use for the CrowdSec Middleware."""
    return f"{_context(host)['k8s_namespace']}-crowdsec@kubernetescrd"


def _entrypoint_for_port_name(host: str, port_name: str) -> str:
    """Resolve a container port NAME to the Traefik entrypoint listening on it.

    A probe names a port; an entrypoint names an address. Nothing states the mapping, so it is
    derived: the container port's number, matched against the entrypoints' `:<port>` addresses.
    """
    ports = {p["name"]: p["containerPort"] for p in _traefik_container(host)["ports"]}
    assert port_name in ports, (
        f"container has no port named {port_name!r}: {sorted(ports)}"
    )
    number = ports[port_name]
    matches = [
        name
        for name, spec in _static_config(host)["entryPoints"].items()
        if spec["address"].rsplit(":", 1)[-1] == str(number)
    ]
    assert len(matches) == 1, (
        f"port {port_name} ({number}) maps to entrypoints {matches}, expected exactly one"
    )
    return matches[0]


def selfcheck_gaps(
    probe_entrypoint: str,
    probe_path: str,
    route_entrypoints: list[str],
    route_rules: list[str],
    entrypoint_chains: dict[str, list[str]],
    crowdsec_ref: str,
) -> list[str]:
    """The comparison itself, taking plain arguments so the rejecting tests can drive it.

    Returns one message per way the probe has stopped detecting the #1322 failure; empty means
    the coupling holds.
    """
    out = []
    if probe_entrypoint not in route_entrypoints:
        out.append(
            f"the startupProbe hits the {probe_entrypoint!r} entrypoint but the self-check "
            f"route serves {route_entrypoints} — the probe reads some other router."
        )
    if not any(probe_path in rule for rule in route_rules):
        out.append(
            f"no self-check route rule matches the probe path {probe_path!r}: {route_rules} — "
            f"the probe 404s on a healthy edge, or matches an app's router instead."
        )
    for entrypoint in route_entrypoints:
        if crowdsec_ref not in entrypoint_chains.get(entrypoint, []):
            out.append(
                f"entrypoint {entrypoint!r} does not carry {crowdsec_ref} in its middleware "
                f"chain, so a missing bouncer plugin no longer rejects the self-check router "
                f"— the probe would pass through the #1322 outage."
            )
    return out


def test_the_probe_detects_a_missing_bouncer_plugin() -> None:
    """The coupling, on the rendered manifests: probe -> route -> crowdsec-bearing entrypoint."""
    probe = _traefik_container(_HOST).get("startupProbe")
    assert probe, (
        "traefik has no startupProbe — /ping alone reports 200 with zero routers (#1322)"
    )
    route = _selfcheck_route(_HOST)
    config = _static_config(_HOST)
    chains = {
        # `or []`, not a default: dropping the last entry leaves a bare `middlewares:` key,
        # which parses to None. `.get(..., [])` returns that None and `selfcheck_gaps` raises
        # a TypeError instead of naming the gap. Matches the sibling guard in
        # test_traefik_http_entrypoint_crowdsec.py (#1355).
        name: (spec.get("http") or {}).get("middlewares") or []
        for name, spec in config["entryPoints"].items()
    }
    problems = selfcheck_gaps(
        probe_entrypoint=_entrypoint_for_port_name(_HOST, probe["httpGet"]["port"]),
        probe_path=probe["httpGet"]["path"],
        route_entrypoints=route["spec"]["entryPoints"],
        route_rules=[r["match"] for r in route["spec"]["routes"]],
        entrypoint_chains=chains,
        crowdsec_ref=_crowdsec_middleware_ref(_HOST),
    )
    assert not problems, f"{_HOST}: " + " ".join(problems)


@pytest.mark.parametrize(
    "kwargs,expected_fragment",
    [
        pytest.param(
            {"route_entrypoints": ["http"]},
            "reads some other router",
            id="probe_and_route_on_different_entrypoints",
        ),
        pytest.param(
            {"probe_path": "/ping"},
            "404s on a healthy edge",
            id="probe_path_is_not_the_selfcheck_route",
        ),
        pytest.param(
            {"entrypoint_chains": {"https": ["homelab-compress@kubernetescrd"]}},
            "no longer rejects the self-check router",
            id="crowdsec_dropped_from_the_entrypoint_chain",
        ),
    ],
)
def test_a_broken_coupling_is_flagged(
    kwargs: dict[str, Any], expected_fragment: str
) -> None:
    """The rejecting half. Each case is green from the passing side alone — the probe still
    returns 200 on a healthy edge in every one of them, and detects nothing."""
    args: dict[str, Any] = {
        "probe_entrypoint": "https",
        "probe_path": "/.well-known/traefik-edge-selfcheck",
        "route_entrypoints": ["https"],
        "route_rules": ["PathPrefix(`/.well-known/traefik-edge-selfcheck`)"],
        "entrypoint_chains": {"https": ["homelab-crowdsec@kubernetescrd"]},
        "crowdsec_ref": "homelab-crowdsec@kubernetescrd",
    }
    assert not selfcheck_gaps(**args), "the control arguments must be clean"
    problems = selfcheck_gaps(**{**args, **kwargs})
    assert problems, f"{kwargs} was not flagged"
    assert any(expected_fragment in p for p in problems), problems


def test_the_route_has_no_host_matcher() -> None:
    """A kubelet httpGet probe connects to the pod IP and sends no SNI, and Traefik answers a
    matched Host() router whose SNI disagrees with the Host header with 421 — measured
    2026-09-06 against this route before it went path-only. A probe that can only return 421
    or 404 never passes, so the edge would crashloop on the change meant to protect it."""
    for rule in (r["match"] for r in _selfcheck_route(_HOST)["spec"]["routes"]):
        assert "Host(" not in rule, (
            f"self-check rule {rule!r} matches on Host — an SNI-less probe gets 421, not 200"
        )


def test_the_route_is_a_traefik_service_not_a_kubernetes_one() -> None:
    """`ping@internal` needs `kind: TraefikService` and no port. Give it the default kind and
    Traefik looks for a k8s Service of that name, finds nothing, and the route 404s silently —
    the trap dashboard-ingressroute.yaml.j2 records, and here it would wedge the edge."""
    for service in _selfcheck_route(_HOST)["spec"]["routes"][0]["services"]:
        assert service["kind"] == "TraefikService", service
        assert "port" not in service, service


def test_the_probe_is_absent_without_crowdsec() -> None:
    """The probe is gated on traefik_k8s_manage_crowdsec. A cluster without the bouncer plugin
    has no crowdsec Middleware and no #1322 failure mode, so the probe would only be a way for
    the edge to crashloop — and defaults/main.yml is explicit that a false failure on staging
    is a real hazard."""
    assert _traefik_container(_HOST_WITHOUT_CROWDSEC).get("startupProbe") is None


def test_the_route_ships_only_where_the_probe_does() -> None:
    """The route is gated in tasks/main.yml rather than in the template, the same way the ACME
    manifests are, so tasks/main.yml is where its pairing with the probe is stated."""
    tasks = (K8S_ROLES / _ROLE / "tasks" / "main.yml").read_text()
    line = next(
        (ln for ln in tasks.splitlines() if "edge-selfcheck-ingressroute.yaml" in ln),
        None,
    )
    assert line, (
        "tasks/main.yml no longer ships edge-selfcheck-ingressroute.yaml at all"
    )
    assert "traefik_k8s_manage_crowdsec" in line, (
        f"the self-check route is not gated on traefik_k8s_manage_crowdsec: {line.strip()!r}"
    )
