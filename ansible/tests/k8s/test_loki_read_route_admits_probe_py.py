"""loki-homelab's read route must admit `probe.py` from EITHER node's shell.

`probe.py loki-query` and `probe.py loki-labels` are the only callers of this route — every
in-cluster reader reaches Loki on the ClusterIP (see the template's own comment). probe.py runs
as a host process, and a host process reaching the ingress VIP arrives at Traefik as one of two
cluster-internal addresses: its node's cni0 gateway when the traefik pod is on that same node,
its node's flannel.1 address when the pod is on the other one.

The route admitted the cni0 pair alone until 2026-09-10, so from daniel-server — where traffic
crosses the overlay to daniel-box's pinned traefik pod — every request 404'd (#1693). A 404 is
Traefik reporting that no router matched, which is indistinguishable from a broken query to the
operator reading it, and this route has no other caller to notice.

Run: uv run pytest ansible/tests/k8s/test_loki_read_route_admits_probe_py.py
"""

from lib import yaml_fast
from _manifest_guards import ALL_VARS, K8S, _k8s_entries, _render, _role_defaults

ROLE = "loki-homelab"
# The route object this guard is about, named rather than globbed: the role renders a second
# monitoring route (the Pi's push door), and a census that lost the read route would assert
# nothing while still passing.
READ_ROUTE = "loki-homelab-monitoring"


def _read_route_match() -> str:
    entry = next(c for c in _k8s_entries() if c["name"] == ROLE)
    rendered = _render(
        K8S / ROLE / "templates" / "ingressroute.yaml.j2",
        container_item=entry,
        domain="example.com",
        **_role_defaults(ROLE),
    )
    docs = [d for d in yaml_fast.safe_load_all(rendered) if d]
    named = {d["metadata"]["name"]: d for d in docs}
    assert READ_ROUTE in named, (
        f"no {READ_ROUTE} in the role's rendered ingressroute.yaml — every assertion below "
        f"would have passed while checking nothing; rendered {sorted(named)}"
    )
    return named[READ_ROUTE]["spec"]["routes"][0]["match"]


def _unadmitted(cidrs) -> list[str]:
    """The members of `cidrs` the read route does NOT admit as a client."""
    match = _read_route_match()
    return [cidr for cidr in cidrs if f"ClientIP(`{cidr}`)" not in match]


def test_the_route_admits_both_host_source_families_is_clean():
    wanted = ALL_VARS["k3s_cni0_gateways"] + ALL_VARS["k3s_flannel_node_ips"]
    assert not _unadmitted(wanted), (
        f"the loki read route does not admit {_unadmitted(wanted)}. probe.py runs from a node "
        "shell and arrives as one of these four addresses depending on which node the traefik "
        "pod sits on; a missing one means `probe.py loki-query` answers 404 from that node "
        "(#1693)."
    )


def test_the_cni0_pair_alone_is_flagged():
    """The rejecting half — the exact set the route carried before #1693.

    Both halves have to be real addresses for this to mean anything: if the flannel pair were
    dropped from the inventory this test would still pass while the one above stopped checking
    the thing it names.
    """
    assert ALL_VARS["k3s_flannel_node_ips"], "k3s_flannel_node_ips is empty or missing"
    assert _unadmitted(["10.42.0.0/16"]), (
        "the route admits the whole pod CIDR again — the DECIDED 2026-08-24 marker in the "
        "template says why that gave every pod the full Loki API of an auth_enabled:false "
        "instance"
    )


def test_the_path_prefix_still_covers_what_probe_py_asks_for():
    """The other half of a 404: the prefix. probe.py reads /labels and /query_range."""
    match = _read_route_match()
    assert "PathPrefix(`/loki/api/v1/`)" in match, match
