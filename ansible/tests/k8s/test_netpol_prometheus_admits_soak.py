"""prometheus-callers admits the soak Job from flux-system on 9090, as ONE `from` item.

D5.3's soak Job runs in `flux-system` and reads Prometheus on 9090
(`docs/target-architecture/deploy/d5-bulk-move.md`, *Reaching Prometheus*). The peer in
netpol-baseline's `networkpolicy-prometheus.yaml.j2` must AND the namespace with the pod label
`app: soak`. Written as two `from` items the pair ORs instead: the namespaceSelector alone
admits every pod in flux-system, and the podSelector alone admits an `app: soak` pod in
observability. Both shapes render a valid policy, so only a reader of the peer's shape tells
them apart. The rejecting half splits the rendered peer in two and asserts the reader refuses it.

Run: uv run pytest ansible/tests/k8s/test_netpol_prometheus_admits_soak.py
"""

import copy

from _k8s_render import render_role_template
from lib import yaml_fast

PORT = 9090
NAMESPACE = {"matchLabels": {"kubernetes.io/metadata.name": "flux-system"}}
POD = {"matchLabels": {"app": "soak"}}


def _policy() -> dict:
    text = render_role_template(
        "netpol-baseline",
        "networkpolicy-prometheus.yaml.j2",
        {"netpol_baseline_obs_enforced": True},
    )
    docs = [d for d in yaml_fast.safe_load_all(text) if d]
    assert len(docs) == 1 and docs[0]["metadata"]["name"] == "prometheus-callers", docs
    return docs[0]


def _admits_soak(policy: dict) -> bool:
    """True when one rule on TCP 9090 holds one peer naming both flux-system and `app: soak`."""
    return any(
        {"protocol": "TCP", "port": PORT} in rule.get("ports", [])
        and {"namespaceSelector": NAMESPACE, "podSelector": POD} in rule.get("from", [])
        for rule in policy["spec"]["ingress"]
    )


def test_soak_peer_is_one_item_on_9090():
    assert _admits_soak(_policy())


def test_soak_peer_split_in_two_items_is_refused():
    policy = copy.deepcopy(_policy())
    for rule in policy["spec"]["ingress"]:
        peers = rule["from"]
        index = peers.index({"namespaceSelector": NAMESPACE, "podSelector": POD})
        peers[index : index + 1] = [
            {"namespaceSelector": NAMESPACE},
            {"podSelector": POD},
        ]
    assert not _admits_soak(policy)
