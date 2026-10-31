"""Alertmanager's read door must stay a GET-only, LAN-only read of two API paths (#6691).

`probe.py health` and `probe.py monitors` read silences and inhibitions through it. It carries
no Authelia, so its matcher is the whole control: a dropped `Method(GET)` would let any LAN
host POST a silence that mutes every page, and a missing node address makes `probe.py` 404
from one node's shell (#1693).

Run: uv run pytest ansible/tests/k8s/test_alertmanager_read_route.py
"""

from lib import yaml_fast
from _manifest_guards import ALL_VARS_VALUES, K8S, _render, _role_context

ROLE = "observability"
TEMPLATE = K8S / ROLE / "templates" / "alertmanager-read-ingressroute.yaml.j2"


def _match() -> str:
    rendered = _render(TEMPLATE, **_role_context(ROLE, domain="example.com"))
    (doc,) = [d for d in yaml_fast.safe_load_all(rendered) if d]
    assert doc["metadata"]["name"] == "alertmanager-read", doc["metadata"]
    (route,) = doc["spec"]["routes"]
    assert [m["name"] for m in route.get("middlewares", [])] == ["rate-limit"], route
    return route["match"]


def test_the_door_is_get_only_on_the_two_read_paths():
    match = _match()
    assert "Host(`alertmanager.local.example.com`)" in match, match
    assert "Method(`GET`)" in match, (
        f"{match!r} admits a method other than GET, so a LAN host can POST a silence "
        "through a route with no authentication"
    )
    assert "PathPrefix(`/api/v2/alerts`)" in match, match
    assert "PathPrefix(`/api/v2/silences`)" in match, match
    assert match.count("PathPrefix(") == 2, match


def test_the_door_admits_the_lan_and_both_host_source_families():
    match = _match()
    wanted = (
        [ALL_VARS_VALUES["lan_subnet"]]
        + ALL_VARS_VALUES["k3s_cni0_gateways"]
        + ALL_VARS_VALUES["k3s_flannel_node_ips"]
    )
    missing = [cidr for cidr in wanted if f"ClientIP(`{cidr}`)" not in match]
    assert not missing, (
        f"the read door does not admit {missing}; probe.py arrives as one of the node "
        "addresses depending on which node the traefik pod sits on"
    )
    assert "ClientIP(`10.42.0.0/16`)" not in match, match
