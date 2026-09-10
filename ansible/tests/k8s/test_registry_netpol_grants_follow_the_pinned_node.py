"""The registry policy's two node grants must follow the node its pod is pinned to.

A host process reaching a pod on the OTHER node crosses the VXLAN overlay and arrives as the
sending node's flannel.1 address, not as a cni0 gateway. Measured 2026-09-10 (#1725), with the
pod's node as the only variable: from daniel-box, sonarr's ClusterIP (pod on daniel-box)
answered 200 while prowlarr's (pod on daniel-server) timed out, and from daniel-server the two
verdicts swapped.

The registry survives that because its policy admits BOTH families by hand: daniel-box's cni0
gateway for a same-node pull, and daniel-server's flannel.1 address for the agent's containerd
pulling across the overlay. That pairing is correct only while the registry pod sits on
daniel-box, and nothing tied the two together — `k8s_registry_node` is a nodeSelector in
`deployment.yaml.j2` while the grants are two unrelated role defaults. Repointing the pin at
daniel-server would leave daniel-box's own pulls arriving as `10.42.0.0` and denied, with the
selftest job the only thing to say so.

Run: uv run pytest ansible/tests/k8s/test_registry_netpol_grants_follow_the_pinned_node.py
"""

from lib import yaml_fast
from _manifest_guards import ALL_VARS, K8S, _render, _role_defaults

ROLE = "registry"
# The order both address lists are written in, stated here so an inventory reordering fails a
# test rather than silently inverting which node each grant belongs to. The /24 agreement
# asserted below is what actually ties an index to a node.
PROD_NODES = ("daniel-box", "daniel-server")

DEFAULTS = _role_defaults(ROLE)
CNI0 = ALL_VARS["k3s_cni0_gateways"]
FLANNEL = ALL_VARS["k3s_flannel_node_ips"]


def _slash24(cidr: str) -> str:
    return ".".join(cidr.split("/")[0].split(".")[:3])


def _expected_grants(pinned: str) -> tuple[str, str]:
    """The (cni0, flannel) pair a registry pinned to `pinned` has to admit.

    The pinned node's own cni0 gateway, because a pull from that node's containerd never leaves
    the bridge; and every OTHER node's flannel.1 address, because that is what a pull from there
    arrives as. Two nodes, so the second half is a single address.
    """
    i = PROD_NODES.index(pinned)
    return CNI0[i], FLANNEL[1 - i]


def test_the_two_address_lists_are_indexed_by_the_same_node():
    """Non-vacuity, and the fact every assertion below rests on.

    Both lists carry one entry per prod node in the same order. Their /24s agree per index
    because a node's cni0 gateway and its flannel.1 address both sit in that node's pod CIDR
    (10.42.<n>.1 and 10.42.<n>.0), so a reordering of one list alone shows up here.
    """
    assert len(CNI0) == len(FLANNEL) == len(PROD_NODES), (CNI0, FLANNEL)
    assert [_slash24(c) for c in CNI0] == [_slash24(f) for f in FLANNEL], (
        CNI0,
        FLANNEL,
    )
    assert ALL_VARS["k8s_primary_node"] == PROD_NODES[0], ALL_VARS["k8s_primary_node"]


def test_the_grants_match_the_pinned_node_is_clean():
    pinned = DEFAULTS["k8s_registry_node"]
    assert pinned in PROD_NODES, (
        f"the registry is pinned to {pinned!r}, which is not one of the prod nodes this guard "
        f"knows the addresses of ({PROD_NODES}) — add it to both inventory lists and here"
    )
    cni0, flannel = _expected_grants(pinned)
    assert DEFAULTS["registry_k8s_netpol_cni_gateway"] == cni0, (
        f"registry pinned to {pinned} admits {DEFAULTS['registry_k8s_netpol_cni_gateway']} as "
        f"its cni0 gateway, but that node's gateway is {cni0} — a same-node containerd pull "
        f"arrives as {cni0} and would be denied (#1725)"
    )
    assert DEFAULTS["registry_k8s_netpol_agent_flannel"] == flannel, (
        f"registry pinned to {pinned} admits {DEFAULTS['registry_k8s_netpol_agent_flannel']} as "
        f"the overlay source, but the OTHER node arrives as {flannel} — a cross-node pull would "
        f"be denied while the selftest job is the only caller that says so (#1725)"
    )


def test_the_pair_for_the_other_node_is_flagged():
    """The rejecting half: the same grants read against the other pin must NOT satisfy it.

    Without this, the assertions above would pass for any pin whose expected pair happened to
    equal the committed one — which is what a check insensitive to the thing it guards looks
    like from the passing side.
    """
    other = (
        PROD_NODES[1]
        if DEFAULTS["k8s_registry_node"] == PROD_NODES[0]
        else PROD_NODES[0]
    )
    cni0, flannel = _expected_grants(other)
    committed = (
        DEFAULTS["registry_k8s_netpol_cni_gateway"],
        DEFAULTS["registry_k8s_netpol_agent_flannel"],
    )
    assert committed != (cni0, flannel), (
        f"the committed grants {committed} are the pair for a registry pinned to {other}, not to "
        f"{DEFAULTS['k8s_registry_node']}"
    )


def test_the_rendered_policy_carries_both_grants():
    """The defaults agreeing is not the policy admitting — the template has to name both."""
    rendered = _render(
        K8S / ROLE / "templates" / "networkpolicy.yaml.j2",
        domain="example.com",
        **DEFAULTS,
    )
    docs = [d for d in yaml_fast.safe_load_all(rendered) if d]
    cidrs = {
        peer["ipBlock"]["cidr"]
        for doc in docs
        for rule in doc["spec"].get("ingress", [])
        for peer in rule.get("from", [])
        if "ipBlock" in peer
    }
    assert cidrs, (
        f"the rendered registry policy carries no ipBlock at all: {rendered[:400]}"
    )
    for grant in (
        DEFAULTS["registry_k8s_netpol_cni_gateway"],
        DEFAULTS["registry_k8s_netpol_agent_flannel"],
    ):
        assert grant in cidrs, (
            f"{grant} is a role default but no rendered rule admits it: {sorted(cidrs)}"
        )
