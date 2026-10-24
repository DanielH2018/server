"""The registry policy's two node grants must follow the node its pod is pinned to.

A host process reaching a pod on the OTHER node crosses the VXLAN overlay and arrives as the
sending node's flannel.1 address, not as a cni0 gateway. With the pod's node as the only
variable, from daniel-box sonarr's ClusterIP (pod on daniel-box) answers 200 while prowlarr's
(pod on daniel-server) times out, and from daniel-server the two verdicts swap.

The registry survives that because its policy admits BOTH families by hand: daniel-box's cni0
gateway for a same-node pull, and daniel-server's flannel.1 address for the agent's containerd
pulling across the overlay. That pairing is correct only while the registry pod sits on
daniel-box, and nothing tied the two together — `k8s_registry_node` is a nodeSelector in
`deployment.yaml.j2` while the grants are two unrelated role defaults. Repointing the pin at
daniel-server would leave daniel-box's own pulls arriving as `10.42.0.0` and denied, with the
selftest job the only thing to say so.

The module also holds the policy's two other grants that a mistake would open or close
silently: the ghcr.io mirror port, and source-controller's standing grant for break-glass.

Run: uv run pytest ansible/tests/k8s/test_registry_netpol_grants_follow_the_pinned_node.py
"""

from lib import yaml_fast
from _manifest_guards import ALL_VARS_VALUES, K8S, _render, _role_context

ROLE = "registry"
# The order both address lists are written in, stated here so an inventory reordering fails a
# test rather than silently inverting which node each grant belongs to. The /24 agreement
# asserted below is what actually ties an index to a node.
PROD_NODES = ("daniel-box", "daniel-server")

DEFAULTS = _role_context(ROLE)
CNI0 = ALL_VARS_VALUES["k3s_cni0_gateways"]
FLANNEL = ALL_VARS_VALUES["k3s_flannel_node_ips"]


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
    assert ALL_VARS_VALUES["k8s_primary_node"] == PROD_NODES[0], ALL_VARS_VALUES[
        "k8s_primary_node"
    ]


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
        **_role_context(ROLE, domain="example.com"),
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


def _policies() -> dict[str, dict]:
    ctx = _role_context(ROLE, domain="example.com")
    return {
        d["metadata"]["name"]: d
        for template in ("networkpolicy.yaml.j2", "ghcr-mirror.yaml.j2")
        for d in yaml_fast.safe_load_all(
            _render(K8S / ROLE / "templates" / template, **ctx)
        )
        if d and d["kind"] == "NetworkPolicy"
    }


def test_the_ghcr_mirror_port_is_admitted_only_from_the_node_grants():
    """containerd pulls through the ghcr.io mirror from the same two host sources.

    The mirror is its own pod, so its own policy has to admit them. A mirror port missing from
    it drops every pull, containerd falls back to ghcr.io without a word, and the mirror stays
    empty. A pod-network rule that admitted it would let any admitted pod fill the mirror's
    claim with arbitrary public images.
    """
    mirror = DEFAULTS["k8s_registry_ghcr_mirror_port"]
    admitting = [
        (name, rule)
        for name, policy in _policies().items()
        for rule in policy["spec"]["ingress"]
        if mirror in {p["port"] for p in rule.get("ports", [])}
    ]
    assert [name for name, _ in admitting] == ["ghcr-mirror"], (
        f"port {mirror} is admitted by {admitting}"
    )
    assert _policies()["ghcr-mirror"]["spec"]["podSelector"] == {
        "matchLabels": {"app": "ghcr-mirror"}
    }
    peers = admitting[0][1]["from"]
    assert all("ipBlock" in peer for peer in peers), peers
    assert {peer["ipBlock"]["cidr"] for peer in peers} == {
        DEFAULTS["registry_k8s_netpol_cni_gateway"],
        DEFAULTS["registry_k8s_netpol_agent_flannel"],
    }, peers


def _vendored_source_controller_pod_labels() -> dict[str, str]:
    components = (K8S / "flux" / "files" / "gotk-components.yaml").read_text()
    (deployment,) = [
        d
        for d in yaml_fast.safe_load_all(components)
        if d
        and d["kind"] == "Deployment"
        and d["metadata"]["name"] == "source-controller"
    ]
    return deployment["spec"]["template"]["metadata"]["labels"]


def test_source_controller_reaches_the_push_port_and_nothing_else_does():
    """Break-glass points Flux's OCIRepository at this registry (#6046).

    source-controller fetches the artifact before Flux applies anything in it, so the grant has
    to stand rather than arrive with the break-glass render. The peer must AND the namespace and
    the pod label in ONE item: as two items it would admit every flux-system pod, and any pod
    labelled `app: source-controller` in the registry's own namespace. The labels are read from
    the vendored install, so a Flux regeneration that renames either breaks this test, not the
    break-glass fetch.
    """
    flux_namespace = _role_context("flux")["flux_namespace"]
    pod_labels = _vendored_source_controller_pod_labels()
    flux_peers = [
        (rule, peer)
        for rule in _policies()["registry"]["spec"]["ingress"]
        for peer in rule["from"]
        if "namespaceSelector" in peer
    ]
    assert len(flux_peers) == 1, flux_peers
    rule, peer = flux_peers[0]
    assert peer["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": flux_namespace}
    }, peer
    selector = peer.get("podSelector", {}).get("matchLabels")
    assert selector, (
        f"the flux-system peer selects no pod, so it admits the namespace: {peer}"
    )
    assert selector.items() <= pod_labels.items(), (selector, pod_labels)
    assert [p["port"] for p in rule["ports"]] == [DEFAULTS["k8s_registry_port"]], rule
