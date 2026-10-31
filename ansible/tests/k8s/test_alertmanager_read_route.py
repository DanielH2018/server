"""Alertmanager's two routes on alertmanager.local keep their split (#6691, #6690).

The read door is a GET-only, LAN-only read of two API paths. `probe.py health` and `probe.py
monitors` read silences and inhibitions through it. It carries no Authelia, so its matcher is
the whole control: a dropped `Method(GET)` would let any LAN host POST a silence that mutes
every page, and a missing node address makes `probe.py` 404 from one node's shell (#1693).

The UI route takes every other request on the host, behind the `authelia` chain. It must lose
to the read door on the door's paths, or Authelia's redirect breaks `probe.py`.

Run: uv run pytest ansible/tests/k8s/test_alertmanager_read_route.py
"""

from lib import yaml_fast
from _manifest_guards import ALL_VARS_VALUES, K8S, _render, _role_context

ROLE = "observability"
TEMPLATES = K8S / ROLE / "templates"
TEMPLATE = TEMPLATES / "alertmanager-read-ingressroute.yaml.j2"
UI_TEMPLATE = TEMPLATES / "alertmanager-ingressroute.yaml.j2"


def _docs(template, **overrides) -> list[dict]:
    ctx = _role_context(ROLE, domain="example.com", **overrides)
    return [d for d in yaml_fast.safe_load_all(_render(template, **ctx)) if d]


def _route(template, name: str, middlewares: list[str], **overrides) -> dict:
    (doc,) = _docs(template, **overrides)
    assert doc["metadata"]["name"] == name, doc["metadata"]
    (route,) = doc["spec"]["routes"]
    assert [m["name"] for m in route.get("middlewares", [])] == middlewares, route
    return route


def _match() -> str:
    return _route(TEMPLATE, "alertmanager-read", ["rate-limit"])["match"]


def _ui_route(**overrides) -> dict:
    return _route(UI_TEMPLATE, "alertmanager", ["rate-limit", "authelia"], **overrides)


def _priority(route: dict) -> int:
    """Traefik's ranking: an explicit `priority`, else the rule's length."""
    return route.get("priority", len(route["match"]))


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


def test_the_ui_is_behind_authelia_and_lan_only_even_with_public_routes_on():
    route = _ui_route(k8s_public_route=True)
    match = route["match"]
    assert match.startswith("Host(`alertmanager.local.example.com`) && ("), (
        f"{match!r} is not the LAN name alone; a public name would put the silence UI on "
        "the internet"
    )
    assert match.count("Host(") == 1, match
    read_clients = sorted(c for c in _match().split("ClientIP(`")[1:])
    ui_clients = sorted(c for c in match.split("ClientIP(`")[1:])
    assert ui_clients == read_clients, (match, _match())


def test_the_read_door_outranks_the_ui_on_its_paths():
    read = _route(TEMPLATE, "alertmanager-read", ["rate-limit"])
    ui = _ui_route()
    assert _priority(read) > _priority(ui), (
        f"the UI router ({_priority(ui)}) outranks the read door ({_priority(read)}), so a "
        "GET on /api/v2/alerts meets Authelia and probe.py reads a redirect"
    )


def test_the_external_url_is_the_ui_route_with_no_path():
    (deployment,) = [
        d
        for d in _docs(TEMPLATES / "alertmanager.yaml.j2")
        if d["kind"] == "Deployment"
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    urls = [a for a in container["args"] if a.startswith("--web.external-url=")]
    assert urls == ["--web.external-url=https://alertmanager.local.example.com"], (
        f"{urls}: Alertmanager's links must name the UI route's host, and a path would move "
        "--web.route-prefix and with it /-/ready"
    )
    assert "Host(`alertmanager.local.example.com`)" in _ui_route()["match"]
