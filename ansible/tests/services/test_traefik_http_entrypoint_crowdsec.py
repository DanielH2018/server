"""The http entrypoint keeps the CrowdSec chain the https entrypoint carries.

Traefik's startup error lines

    {"level":"error","entryPointName":"http","routerName":"http-to-443@internal",
     "error":"middleware \\"homelab-crowdsec@kubernetescrd\\" does not exist"}

do not show that an internal router cannot resolve a `@kubernetescrd` middleware. The line
lands 0-1s after a "Traefik version" startup line, and a middleware that is really
unresolvable repeats it on every configuration event instead. The reference resolves once the
kubernetesCRD watch delivers, so the chain is live enforcement, and dropping it from the http
entrypoint would reject a banned IP one request later than it is rejected today. This guard
keeps the chain in place.

Checked on the RENDERED static config, not the template's text — the indirection trap in
`textual-guard-checks-break-on-indirection`, and the same reason as the sibling
test_traefik_edge_selfcheck.py. Both read it through `_k8s_render.traefik_static_config`.
"""

from typing import Any

import pytest


from _k8s_render import host_context, traefik_static_config

# daniel-box runs the bouncer plugin. A cluster with traefik_k8s_manage_crowdsec false renders
# no chain at all on either entrypoint — covered by its own test below. That case is an
# override rather than a host.
_HOST = "daniel-box"
_WITHOUT_CROWDSEC = {"traefik_k8s_manage_crowdsec": False}


def _chains(host: str, overrides: dict | None = None) -> dict[str, list[str]]:
    config = traefik_static_config(overrides, host=host)
    return {
        # `or []`, not a default: dropping the last entry leaves a bare `middlewares:` key,
        # which parses to None. `.get(..., [])` returns that None and the comparison below
        # raises a TypeError instead of reporting the gap.
        name: (spec.get("http") or {}).get("middlewares") or []
        for name, spec in config["entryPoints"].items()
    }


def crowdsec_chain_gaps(
    entrypoint_chains: dict[str, list[str]], crowdsec_ref: str
) -> list[str]:
    """The comparison itself, taking plain arguments so the rejecting tests can drive it.

    Returns one message per way the http entrypoint has stopped enforcing crowdsec; empty
    means the posture holds. The https entrypoint is the reference rather than a hardcoded
    expectation: turning the bouncer off entirely is a supported configuration, and this
    guard must say nothing about that case.
    """
    out = []
    for entrypoint in ("http", "https"):
        if entrypoint not in entrypoint_chains:
            out.append(f"entrypoint {entrypoint!r} is missing entirely")
    if out:
        return out

    https_enforces = crowdsec_ref in entrypoint_chains["https"]
    http_enforces = crowdsec_ref in entrypoint_chains["http"]
    if https_enforces and not http_enforces:
        out.append(
            f"the https entrypoint chains {crowdsec_ref} but the http entrypoint does not, so "
            f"a banned IP is admitted to http-to-443@internal and rejected only on its second "
            f"request. The one or two startup 'does not exist' lines are a provider-ordering "
            f"window, not a reason to drop it — see #1343 and this file's docstring."
        )
    if http_enforces and not https_enforces:
        out.append(
            f"the http entrypoint chains {crowdsec_ref} but the https entrypoint — where every "
            f"route in this repo lives — does not, so nothing is actually protected."
        )
    return out


def test_the_http_entrypoint_enforces_crowdsec() -> None:
    """The accepting half, on the rendered static config for the host that runs the bouncer."""
    chains = _chains(_HOST)
    crowdsec_ref = f"{host_context(_HOST)['k8s_namespace']}-crowdsec@kubernetescrd"
    # Non-vacuity: a guard that finds no crowdsec reference anywhere would pass silently.
    assert crowdsec_ref in chains["https"], (
        f"{_HOST}: the https entrypoint does not chain {crowdsec_ref} — this guard's "
        f"reference point is gone and it is checking nothing: {chains}"
    )
    problems = crowdsec_chain_gaps(chains, crowdsec_ref)
    assert not problems, f"{_HOST}: " + " ".join(problems)


def test_a_cluster_without_the_bouncer_is_not_flagged() -> None:
    """traefik_k8s_manage_crowdsec false renders no chain on either entrypoint, and that is
    a supported configuration rather than a gap."""
    chains = _chains(_HOST, _WITHOUT_CROWDSEC)
    crowdsec_ref = f"{host_context(_HOST)['k8s_namespace']}-crowdsec@kubernetescrd"
    assert crowdsec_ref not in chains["https"], (
        "the flag no longer removes the chain; this test's premise is stale"
    )
    assert not crowdsec_chain_gaps(chains, crowdsec_ref)


@pytest.mark.parametrize(
    "chains,expected_fragment",
    [
        pytest.param(
            {
                "http": ["homelab-compress@kubernetescrd"],
                "https": ["homelab-crowdsec@kubernetescrd"],
            },
            "rejected only on its second request",
            id="crowdsec_dropped_from_the_http_chain",
        ),
        pytest.param(
            {"http": [], "https": ["homelab-crowdsec@kubernetescrd"]},
            "rejected only on its second request",
            id="http_chain_emptied",
        ),
        pytest.param(
            {"http": ["homelab-crowdsec@kubernetescrd"], "https": []},
            "nothing is actually protected",
            id="crowdsec_dropped_from_the_https_chain",
        ),
        pytest.param(
            {"https": ["homelab-crowdsec@kubernetescrd"]},
            "'http' is missing entirely",
            id="http_entrypoint_removed",
        ),
    ],
)
def test_a_weakened_posture_is_flagged(
    chains: dict[str, Any], expected_fragment: str
) -> None:
    """The rejecting half. Every case here still serves a working 301 on http and a working
    edge on https, so none of them is visible from the passing side alone."""
    crowdsec_ref = "homelab-crowdsec@kubernetescrd"
    control = {
        "http": ["homelab-crowdsec@kubernetescrd"],
        "https": ["homelab-crowdsec@kubernetescrd"],
    }
    assert not crowdsec_chain_gaps(control, crowdsec_ref), (
        "the control arguments must be clean"
    )
    problems = crowdsec_chain_gaps(chains, crowdsec_ref)
    assert problems, f"{chains} was not flagged"
    assert any(expected_fragment in p for p in problems), problems
