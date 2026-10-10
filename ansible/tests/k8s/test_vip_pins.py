#!/usr/bin/env python3
"""Every VIP-backed workload must be pinned to the node that announces the VIP.

WHY THIS IS A TEST AND NOT A COMMENT. MetalLB's L2Advertisement announces from
daniel-box alone (roles/setup/k3s/templates/metallb-pool.yaml.j2 documents why that is
permanent). With `externalTrafficPolicy: Local`, the announcing node forwards a packet
only to a backend on *itself* — so a VIP-backed pod scheduled onto daniel-server makes
its own service black-hole every packet.

The failure is silent in the worst way: the pod passes its probes, `kubectl get pods`
shows Running, and only traffic from the LAN disappears. A VIP-backed workload with no pin
survives only while the scheduler happens to place it correctly.

A comment cannot catch the next one. This can: add a `type: LoadBalancer` service with
ETP Local and no pin, and the suite fails.

Both halves are read off RENDERS — the L2Advertisement through `_setup_render`, the Services and
workloads through `_k8s_render` — once at inventory values and once with `k8s_primary_node` set
to a sentinel. The second render is what proves the two halves move as one unit: a pin that
names the node some other way agrees with the announcement today and stops agreeing the day
the variable changes, and only a render at a value the inventory does not hold shows that.

Run: uv run pytest ansible/tests/k8s/test_vip_pins.py
"""

from lib import yaml_fast

from _k8s_render import render_texts, rendered_docs
from lib.ansible_jinja_env import template_env
from lib.render_guard import render_or_error
from _setup_render import role_context, rendered_setup_text
from lib.repo_paths import K3S_ROLE

# The variable the announcement and every workload pin read.
PRIMARY_NODE_VAR = "k8s_primary_node"
# A node name no inventory holds, so a pin that reaches it can only have come from the variable.
SENTINEL_NODE = "sentinel-vip-node"

_HOSTNAME = "kubernetes.io/hostname"
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}

# jellyfin is pinned by storage, not by its own nodeSelector: the media-volume `local` PV
# declares a required nodeAffinity on the node it lives on, which the scheduler enforces
# just as hard. Listed explicitly rather than special-cased in the logic so that moving the
# media volume off node-local storage forces someone to revisit this line.
PINNED_BY_VOLUME = {"jellyfin"}

# Roles the census must find, so a matcher that stopped matching names the member it lost
# instead of passing over an empty list.
KNOWN_VIP_ROLES = frozenset(
    {"jellyfin", "mosquitto", "pihole", "terraria", "traefik", "valheim", "wg-easy"}
)


def _announcing_nodes(metallb_pool_text: str) -> set[str]:
    """Every hostname the rendered L2Advertisements announce from."""
    nodes = {
        selector["matchLabels"][_HOSTNAME]
        for doc in yaml_fast.safe_load_all(metallb_pool_text)
        if doc and doc.get("kind") == "L2Advertisement"
        for selector in doc["spec"].get("nodeSelectors", [])
    }
    assert nodes, (
        "no L2Advertisement nodeSelector hostname rendered — check the matcher"
    )
    return nodes


def _sentinel_metallb_pool() -> str:
    """metallb-pool.yaml.j2 alone, in setup/k3s's context with the sentinel laid on top."""
    text, err = render_or_error(
        template_env(K3S_ROLE / "templates"),
        "metallb-pool.yaml.j2",
        role_context(K3S_ROLE, {PRIMARY_NODE_VAR: SENTINEL_NODE}),
    )
    assert text is not None, f"setup/k3s/metallb-pool.yaml.j2 failed to render: {err}"
    return text


ANNOUNCING_NODES = _announcing_nodes(rendered_setup_text("k3s", "metallb-pool.yaml.j2"))


def test_announcement_and_the_primary_node_variable_agree():
    """The announcement names the node `k8s_primary_node` names, and follows it.

    Workload pins read `k8s_primary_node`. If they ever disagree with the announcement,
    every VIP-backed pod is pinned away from the announcer and each one black-holes its own
    traffic while reporting healthy — the exact failure the per-workload assertions below
    exist to prevent, arriving through the back door.

    The inventory render alone cannot tell the variable from a literal or a second variable
    that resolves to the same node today. The sentinel render can: only a read of
    `k8s_primary_node` renders the sentinel.
    """
    assert len(ANNOUNCING_NODES) == 1, (
        f"the L2Advertisements announce from {sorted(ANNOUNCING_NODES)}; the workload pins "
        "name one node, so the announcement must too"
    )
    sentinel = _announcing_nodes(_sentinel_metallb_pool())
    assert sentinel == {SENTINEL_NODE}, (
        f"the MetalLB L2Advertisement does not follow {PRIMARY_NODE_VAR}: rendered with it "
        f"set to {SENTINEL_NODE!r}, it still announces from "
        f"{sorted(sentinel)}. Announcement and placement "
        "move as one unit; a second spelling that agrees today is a way to drift tomorrow."
    )


def _vip_roles(docs) -> set[str]:
    """Roles owning a rendered Service that is both type: LoadBalancer and ETP Local."""
    return {
        role
        for role, _tpl, doc in docs
        if doc["kind"] == "Service"
        and doc["spec"].get("type") == "LoadBalancer"
        and doc["spec"].get("externalTrafficPolicy") == "Local"
    }


def _pin_offences(docs, node: str) -> list[str]:
    """One line per VIP-backed workload in `docs` that is not pinned to `node`."""
    docs = list(docs)
    vip_roles = _vip_roles(docs) - PINNED_BY_VOLUME
    offences = []
    for role in sorted(vip_roles):
        workloads = [
            (tpl, doc)
            for r, tpl, doc in docs
            if r == role and doc["kind"] in _WORKLOAD_KINDS
        ]
        if not workloads:
            offences.append(f"{role} owns a VIP Service but no workload to pin")
        for tpl, doc in workloads:
            if doc["kind"] == "DaemonSet":
                continue  # already runs on every node, including the announcer
            pinned = (doc["spec"]["template"]["spec"].get("nodeSelector") or {}).get(
                _HOSTNAME
            )
            if pinned != node:
                offences.append(
                    f"{role}/{tpl} {doc['kind']}/{doc['metadata']['name']} pins "
                    f"{_HOSTNAME}={pinned!r}, not the announcing node {node!r}"
                )
    return offences


def _docs_of(texts):
    for role, tpl, text in texts:
        for doc in yaml_fast.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind"):
                yield role, tpl, doc


def test_some_vip_services_exist():
    """Guard against the discovery logic silently matching nothing."""
    missing = KNOWN_VIP_ROLES - _vip_roles(rendered_docs())
    assert not missing, (
        f"no LoadBalancer + ETP Local Service rendered for {sorted(missing)} — check the "
        "matcher before concluding the role moved off a VIP"
    )


def test_vip_backed_workloads_are_pinned_to_the_announcing_node():
    (node,) = ANNOUNCING_NODES
    offences = _pin_offences(rendered_docs(), node)
    assert offences == [], (
        f"MetalLB announces only from {node}, so a VIP-backed pod on the other node drops "
        f"every packet while it still reports healthy: {offences}"
    )


def test_vip_backed_workloads_follow_the_primary_node_variable():
    """The placement half of the sentinel render: every pin moves with the announcement."""
    docs = _docs_of(render_texts({PRIMARY_NODE_VAR: SENTINEL_NODE}))
    offences = _pin_offences(docs, SENTINEL_NODE)
    assert offences == [], (
        f"with {PRIMARY_NODE_VAR}={SENTINEL_NODE!r} the announcement moves and these pins "
        f"do not — they name the node some other way: {offences}"
    )


def test_an_unpinned_vip_workload_is_flagged():
    service = {
        "kind": "Service",
        "spec": {"type": "LoadBalancer", "externalTrafficPolicy": "Local"},
    }

    def deployment(pod_spec: dict) -> dict:
        return {
            "kind": "Deployment",
            "metadata": {"name": "x"},
            "spec": {"template": {"spec": pod_spec}},
        }

    unpinned = deployment({})
    pinned = deployment({"nodeSelector": {_HOSTNAME: "a"}})
    assert _pin_offences([("x", "s", service), ("x", "d", pinned)], "a") == []
    assert _pin_offences([("x", "s", service), ("x", "d", unpinned)], "a") == [
        f"x/d Deployment/x pins {_HOSTNAME}=None, not the announcing node 'a'"
    ]
    assert _pin_offences([("x", "s", service), ("x", "d", pinned)], "b") != []
