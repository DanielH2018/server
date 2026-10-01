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
test_traefik_edge_selfcheck.py, whose `_static_config` shape this file reuses.
"""

from typing import Any

import pytest


from lib import yaml_fast

from validate.k8s_manifests import (
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
# daniel-box runs the bouncer plugin. A cluster with traefik_k8s_manage_crowdsec false renders
# no chain at all on either entrypoint — covered by its own test below. That case is an
# override rather than a host.
_HOST = "daniel-box"
_WITHOUT_CROWDSEC = {"traefik_k8s_manage_crowdsec": False}


def _context(host: str, overrides: dict | None = None) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    entry = next(c for c in base["containers_list"] if c["name"] == _ROLE)
    # Role defaults FIRST: Ansible ranks host_vars above them. Same ordering as the sibling.
    return {
        **role_defaults(_ROLE, base),
        **base,
        "container_item": entry,
        **(overrides or {}),
    }


def _static_config(host: str, overrides: dict | None = None) -> dict:
    """The Traefik config itself, not the ConfigMap wrapping it.

    static-config.yaml.j2's data value is a block scalar, so the config is a STRING at the
    manifest level and has to be parsed a second time.
    """
    ctx = _context(host, overrides)
    env = make_env([K8S_ROLES / _ROLE / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, "static-config.yaml.j2", ctx)
    assert rendered is not None, (
        f"{_ROLE}/static-config.yaml.j2 failed to render for {host}: {err}"
    )
    doc = yaml_fast.safe_load(rendered)
    return yaml_fast.safe_load(doc["data"]["traefik.yml"])


def _chains(host: str, overrides: dict | None = None) -> dict[str, list[str]]:
    config = _static_config(host, overrides)
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
    crowdsec_ref = f"{_context(_HOST)['k8s_namespace']}-crowdsec@kubernetescrd"
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
    crowdsec_ref = (
        f"{_context(_HOST, _WITHOUT_CROWDSEC)['k8s_namespace']}-crowdsec@kubernetescrd"
    )
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
