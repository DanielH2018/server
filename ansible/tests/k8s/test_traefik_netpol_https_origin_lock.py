"""traefik's :8443 admits Cloudflare, the LAN, the WireGuard clients and the pod CIDR, and nothing else.

Port 8443 is the https entrypoint's POD port, and until #1974 it was open to every source: a
client that resolved the origin IP and sent a public Host header reached the backend without
passing Cloudflare's WAF, with no trusted X-Forwarded-For, and so shared one rate-limit bucket
keyed on "" with every other origin-direct request. The ipBlock list in
networkpolicy-traefik.yaml.j2 is the origin lock; `netpol_baseline_traefik_https_sources` in
the role's defaults composes it from the four inventory ranges.

Four things have to hold, and each drops a different caller if it stops holding:
  - Cloudflare's ranges, or the public names go dark.
  - `lan_subnet`, or every `.local.` route dies from a browser on the LAN.
  - `wg_client_subnet`, or a WireGuard client whose traffic is not masqueraded is locked out.
  - `k3s_pod_cidr`, or the Kuma prober, monitor-bridge and homepage's widgets lose every
    `.local.` name they reach through the VIP.
The rejecting half renders the policy with each source removed in turn and asserts the reader
below names the missing one, so a policy that drops a source fails by name rather than by count.

Run: uv run pytest ansible/tests/k8s/test_traefik_netpol_https_origin_lock.py
"""

import pytest

from lib import yaml_fast
from validate.k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    load_yaml,
    resolve_vars,
    role_defaults,
)
from _manifest_guards import _render

ROLE = "netpol-baseline"
TEMPLATE = K8S_ROLES / ROLE / "templates" / "networkpolicy-traefik.yaml.j2"
SOURCES_VAR = "netpol_baseline_traefik_https_sources"
HTTPS_POD_PORT = 8443
HTTP_POD_PORT = 8000

_BASE = resolve_vars(
    {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)},
    {**BASE_CONTEXT, **load_yaml(ALL_VARS)},
)
CTX = {**_BASE, **role_defaults(ROLE, _BASE)}

# The four sources by the inventory name each comes from, so a failure says which caller
# the policy would drop rather than which index moved.
EXPECTED = {
    "cloudflare_ips": frozenset(CTX["cloudflare_ips"]),
    "lan_subnet": frozenset([CTX["lan_subnet"]]),
    "wg_client_subnet": frozenset([CTX["wg_client_subnet"]]),
    "k3s_pod_cidr": frozenset([CTX["k3s_pod_cidr"]]),
}


def _policy(ctx: dict) -> dict:
    docs = [d for d in yaml_fast.safe_load_all(_render(TEMPLATE, **ctx)) if d]
    assert len(docs) == 1 and docs[0]["kind"] == "NetworkPolicy", docs
    return docs[0]


def _rules_admitting(policy: dict, port: int) -> list[dict]:
    return [
        rule
        for rule in policy["spec"]["ingress"]
        if any(p.get("port") == port for p in rule.get("ports", []))
    ]


def _sources_admitted(policy: dict, port: int) -> frozenset[str] | None:
    """The CIDRs the policy admits to `port`, or None when a rule admits it with no `from:`.

    A rule with no `from:` is open to every source, and a set cannot express that, so it is
    reported as a distinct value rather than as an empty set — an empty set would read as
    "nothing admitted", the opposite of what an open rule does.
    """
    rules = _rules_admitting(policy, port)
    assert rules, f"no ingress rule admits pod port {port}"
    if any("from" not in rule for rule in rules):
        return None
    cidrs = set()
    for rule in rules:
        for peer in rule["from"]:
            assert set(peer) == {"ipBlock"}, (
                f"the :{port} rule admits a peer by identity ({peer}); the origin lock is an "
                "ipBlock list only, because a Cloudflare edge has no pod identity to match"
            )
            assert "except" not in peer["ipBlock"], peer
            cidrs.add(peer["ipBlock"]["cidr"])
    return frozenset(cidrs)


def _missing_sources(admitted: frozenset[str] | None) -> set[str]:
    if admitted is None:
        return set(EXPECTED)
    return {name for name, cidrs in EXPECTED.items() if not cidrs <= admitted}


def test_the_composed_source_list_is_exactly_the_four_inventory_ranges():
    """Non-vacuity, and the fact the render tests below rest on.

    The role default is a Jinja expression over four inventory names; if it stopped resolving
    to a list (a quoting change, a renamed inventory key) every assertion below would be
    reading characters, not CIDRs.
    """
    sources = CTX[SOURCES_VAR]
    assert isinstance(sources, list), type(sources)
    assert len(CTX["cloudflare_ips"]) >= 15, (
        "cloudflare_ips is no longer Cloudflare's full list"
    )
    assert frozenset(sources) == frozenset().union(*EXPECTED.values())
    assert len(sources) == len(set(sources)), "a duplicate CIDR in the source list"


def test_the_https_port_admits_exactly_the_four_sources_is_clean():
    admitted = _sources_admitted(_policy(CTX), HTTPS_POD_PORT)
    assert admitted is not None, (
        f"pod port {HTTPS_POD_PORT} is open to every source again — the origin lock (#1974) "
        "is gone"
    )
    assert not _missing_sources(admitted)
    extra = admitted - frozenset().union(*EXPECTED.values())
    assert not extra, (
        f"the :{HTTPS_POD_PORT} rule admits ranges nothing in the inventory names: {extra}"
    )


@pytest.mark.parametrize("dropped", sorted(EXPECTED))
def test_a_render_that_drops_a_source_is_flagged(dropped: str):
    """The rejecting half: each of the four, removed from the composed list, is named."""
    thinned = [c for c in CTX[SOURCES_VAR] if c not in EXPECTED[dropped]]
    assert len(thinned) < len(CTX[SOURCES_VAR])
    admitted = _sources_admitted(_policy({**CTX, SOURCES_VAR: thinned}), HTTPS_POD_PORT)
    assert _missing_sources(admitted) == {dropped}


def test_the_http_port_stays_open_to_every_source():
    """:8000 is the http->https redirect and the netpol probes' control target.

    Fencing it with the https allow-list would break every probe's control leg (each dials
    traefik:80 to prove it has a network) and turn the redirect into a timeout for the exact
    origin-direct client the :8443 rule is meant to redirect-then-refuse.
    """
    assert _sources_admitted(_policy(CTX), HTTP_POD_PORT) is None, (
        f"pod port {HTTP_POD_PORT} carries a `from:` — the redirect entrypoint and the netpol "
        "probe control legs are fenced"
    )


def test_the_rollback_branch_still_renders_allow_all():
    """`netpol_baseline_enforced: false` is the documented undo, and it must not keep the lock."""
    policy = _policy({**CTX, "netpol_baseline_enforced": False})
    assert policy["spec"]["ingress"] == [{}], policy["spec"]["ingress"]
