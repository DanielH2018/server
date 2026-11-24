"""Alertmanager's read door stays narrow, and Karma, the silence UI, names Authelia on every route.

The read door is a GET-only, LAN-only read of two API paths. `probe.py health` and `probe.py
monitors` read silences and inhibitions through it. It carries no Authelia, so its matcher is
the whole control: a dropped `Method(GET)` would let any LAN host POST a silence that mutes
every page, and a missing node address makes `probe.py` 404 from one node's shell.

Karma can create silences too, behind the `authelia` chain on both its LAN and public names.

Run: uv run pytest tests/k8s/test_alertmanager_read_route.py
"""

from lib import yaml_fast
from _manifest_guards import ALL_VARS_VALUES, K8S, _render, _role_context

TEMPLATE = (
    K8S / "observability" / "templates" / "alertmanager-read-ingressroute.yaml.j2"
)
KARMA_TEMPLATE = K8S / "karma" / "templates" / "ingressroute.yaml.j2"
KARMA_ENTRY = {"name": "karma", "hostname": "karma", "port": 8080, "use_authelia": True}


def _route(role, template, name: str, middlewares: list[str], **overrides) -> dict:
    ctx = _role_context(role, domain="example.com", **overrides)
    (doc,) = [d for d in yaml_fast.safe_load_all(_render(template, **ctx)) if d]
    assert doc["metadata"]["name"] == name, doc["metadata"]
    (route,) = doc["spec"]["routes"]
    assert [m["name"] for m in route.get("middlewares", [])] == middlewares, route
    return route


def _match() -> str:
    return _route("observability", TEMPLATE, "alertmanager-read", ["rate-limit"])[
        "match"
    ]


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


def test_karma_names_authelia_on_both_its_lan_and_public_routes():
    # fact: k8s/roles/karma/CLAUDE.md#Notable
    ctx = _role_context(
        "karma", domain="example.com", k8s_public_route=True, container_item=KARMA_ENTRY
    )
    docs = [d for d in yaml_fast.safe_load_all(_render(KARMA_TEMPLATE, **ctx)) if d]
    matches = {d["metadata"]["name"]: d["spec"]["routes"][0] for d in docs}
    assert set(matches) == {"karma", "karma-public"}, matches
    assert matches["karma"]["match"] == "Host(`karma.local.example.com`)"
    assert matches["karma-public"]["match"] == "Host(`karma.example.com`)"
    for name, route in matches.items():
        assert "authelia" in [m["name"] for m in route["middlewares"]], (
            f"{name} reaches the silence UI without Authelia"
        )
