#!/usr/bin/env python3
"""Guards on the slice-2 strangler bridge (docs/k3s-migration/slice-2-leaf-apps.md).

A migrated service keeps its real hostnames pointing at daniel-server, which forwards them to
the cluster VIP. That arrangement spans two inventories and two Traefiks, and every way it can
be got wrong is quiet:

  * the Docker container left running alongside its bridge -> two routers claim one Host rule
  * a bridge with no k8s route behind it -> 404 on the real hostname
  * the two per-hostname routers merged into one with an || rule -> 421 Misdirected Request,
    but only over the hostname whose SNI lost the coin toss

Run: uv run pytest ansible/tests/test_strangler_bridge.py
"""

from pathlib import Path

import pytest
import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

ANSIBLE = Path(__file__).resolve().parents[1]
HOST_VARS = ANSIBLE / "inventory" / "host_vars"
TRAEFIK = ANSIBLE / "roles" / "containers" / "traefik" / "templates"
DOMAIN = "example.com"
VIP = "10.0.0.240"


def _containers(host: str) -> list[dict]:
    data = yaml.safe_load((HOST_VARS / f"{host}.yml").read_text()) or {}
    return data.get("containers_list") or []


def _bridged() -> list[dict]:
    return [c for c in _containers("daniel-box") if "bridge_hostname" in c]


def _dynamic_config(containers: list[dict] | None = None) -> dict:
    """The traefik file-provider config as Ansible would render it on daniel-server.

    Takes an override list so the multi-service shape can be exercised without waiting for the
    inventory to grow one — every bridge assertion would otherwise be a one-service test.
    """
    env = Environment(
        loader=FileSystemLoader(str(TRAEFIK)),
        undefined=ChainableUndefined,
        keep_trailing_newline=True,
    )
    rendered = env.get_template("config.yml.j2").render(
        domain=DOMAIN,
        k3s_metallb_ingress_vip=VIP,
        hostvars={
            "daniel-box": {
                "containers_list": (
                    _containers("daniel-box") if containers is None else containers
                )
            }
        },
    )
    return yaml.safe_load(rendered)


# Two bridged services, one of them with a hostname that differs from its name (littlelink
# answers on `www`) and one unauthenticated, which is the shape the remaining cutovers take.
TWO_SERVICES = [
    {
        "name": "alpha",
        "bridge_hostname": "alpha",
        "use_authelia": True,
        "bridge_probe_path": "/api/healthcheck",
    },
    {"name": "beta", "bridge_hostname": "www", "use_authelia": False},
    {"name": "unbridged", "use_authelia": True},
]


# Public, unauthenticated paths reproduced on the bridge, keyed by (service, prefix) with the
# reason each one has to stay open. A bypass is a deliberate hole in the edge's authentication,
# so adding one is an edit HERE as well as in inventory — the same shape as
# AUTHELIA_BYPASS_ROUTES in test_k8s_manifests.py.
BRIDGE_BYPASS_PREFIXES = {
    ("healthchecks", "/ping/"): (
        "Every monitored cron POSTs to /ping/<uuid> with no credentials, including two in "
        "initial_setup that hardcode the public URL. Gating it would not fail loudly — every "
        "check would go red while the jobs kept succeeding. Reproduces the healthchecks-ping "
        "Docker label, which died with the container."
    ),
}


def _bypass_routers(config: dict) -> dict[str, dict]:
    return {
        name: router
        for name, router in config["http"]["routers"].items()
        if "-bridge-bypass-" in name
    }


def _probe_routers(config: dict) -> dict[str, dict]:
    return {
        name: router
        for name, router in config["http"]["routers"].items()
        if name.endswith("-bridge-probe")
    }


def _bridge_routers(config: dict) -> dict[str, dict]:
    return {
        name: router
        for name, router in config["http"]["routers"].items()
        if "-bridge-" in name
    }


def test_a_bridged_service_has_no_docker_container_left_running():
    """Both routers would match the same Host rule, and which one wins is Traefik's business,
    not ours. The Docker copy has to be gone before the bridge exists."""
    docker_names = {c["name"] for c in _containers("daniel-server")}
    for svc in _bridged():
        assert svc["name"] not in docker_names, (
            f"{svc['name']} is bridged on daniel-box but still in daniel-server's "
            "containers_list — remove the entry or drop bridge_hostname"
        )


def test_every_bridge_has_a_k8s_route_behind_it():
    """The bridge forwards to the cluster on the unsuffixed hostname, which only resolves to a
    workload if that service's IngressRoute renders the bridged route."""
    for svc in _bridged():
        tpl = (
            ANSIBLE
            / "roles"
            / "k8s"
            / svc["name"]
            / "templates"
            / "ingressroute.yaml.j2"
        )
        assert tpl.exists(), f"{svc['name']} is bridged with no IngressRoute template"
        assert "bridge_hostname" in tpl.read_text(), (
            f"{svc['name']} is bridged but its IngressRoute never passes bridge_hostname to "
            "the macro, so the cluster has no route for its real hostname"
        )


@pytest.mark.parametrize("containers", [None, TWO_SERVICES], ids=["inventory", "two"])
def test_each_bridge_router_matches_exactly_one_host(containers):
    """The 421 trap. The backend is HTTPS, and Traefik rejects a request whose Host header
    disagrees with the SNI it sent upstream. serversTransport.serverName holds one value, so a
    router matching two hostnames can only ever get one of them right."""
    config = _dynamic_config(containers)
    for name, router in _bridge_routers(config).items():
        rule = router["rule"]
        assert rule.count("Host(") == 1, (
            f"{name} matches more than one hostname; each needs its own router and "
            "serversTransport or one of them answers 421"
        )
        assert "||" not in rule, name


@pytest.mark.parametrize("containers", [None, TWO_SERVICES], ids=["inventory", "two"])
def test_each_bridge_sends_an_sni_matching_the_host_it_serves(containers):
    config = _dynamic_config(containers)
    services = config["http"]["services"]
    transports = config["http"]["serversTransports"]

    for name, router in _bridge_routers(config).items():
        host = router["rule"].split("`")[1]
        backend = services[router["service"]]["loadBalancer"]
        assert backend["servers"][0]["url"] == f"https://{VIP}"
        assert transports[backend["serversTransport"]]["serverName"] == host, (
            f"{name} serves {host} but sends a different SNI, which Traefik answers with 421"
        )


def test_bridging_several_services_keeps_their_routers_distinct():
    """Everything above runs against an inventory with one bridged service, where a naming or
    SNI bug that only shows up across services would pass unseen. Three more cutovers follow."""
    config = _dynamic_config(TWO_SERVICES)
    routers = _bridge_routers(config)

    assert set(routers) == {
        "alpha-bridge-public",
        "alpha-bridge-local",
        "alpha-bridge-probe",
        "beta-bridge-public",
        "beta-bridge-local",
    }
    # beta has no bridge_probe_path, so it gets no probe router — the flag is per service.
    hosts = sorted(r["rule"].split("`")[1] for r in routers.values())
    assert hosts == [
        f"alpha.{DOMAIN}",
        f"alpha.local.{DOMAIN}",
        f"alpha.local.{DOMAIN}",
        f"www.{DOMAIN}",
        f"www.local.{DOMAIN}",
    ]
    # use_authelia is per service, not per bridge: beta is public by design.
    assert "authelia" in routers["alpha-bridge-public"]["middlewares"]
    assert "authelia" not in routers["beta-bridge-public"]["middlewares"]


@pytest.mark.parametrize("containers", [None, TWO_SERVICES], ids=["inventory", "two"])
def test_an_authed_bridge_authenticates_at_the_docker_edge(containers):
    """The k8s side of a bridged route deliberately carries no forward-auth, so this is the
    only place the user is authenticated. A bridge that lost it would publish the service."""
    config = _dynamic_config(containers)
    source = _bridged() if containers is None else containers
    authed = {
        c["name"] for c in source if c.get("use_authelia") and "bridge_hostname" in c
    }

    for name, router in _bridge_routers(config).items():
        service = name.rsplit("-bridge-", 1)[0]
        if service not in authed:
            continue
        # A probe router is exempt, and test_a_probe_route_is_never_reachable_from_the_internet
        # is what earns the exemption. Keep the two inseparable: an exemption that stopped
        # depending on the LAN-only rule would be an unauthenticated public path.
        if name in _probe_routers(config):
            continue
        # Likewise for a bypass router, and test_a_bypass_prefix_is_declared_with_a_reason is
        # what earns it. Keep them inseparable: an exemption that stopped depending on the
        # allow-list would let inventory alone open a public unauthenticated path.
        if name in _bypass_routers(config):
            continue
        assert "authelia" in router["middlewares"], (
            f"{name} is the only gate for a use_authelia service and has no forward-auth"
        )


@pytest.mark.parametrize("containers", [None, TWO_SERVICES], ids=["inventory", "two"])
def test_a_probe_route_is_never_reachable_from_the_internet(containers):
    """A probe route is the one bridged route with no forward-auth on either side, so the
    LAN-only host rule is the whole of its protection. There must be no public sibling.

    This is what earns the exemption in the forward-auth test above; the two only make sense
    together, the same way the ClientIP guard and the k8s route's missing authelia do.
    """
    config = _dynamic_config(containers)
    for name, router in _probe_routers(config).items():
        hosts = [h for h in router["rule"].split("`")[1::2] if DOMAIN in h]
        assert hosts, name
        for host in hosts:
            assert host.endswith(f".local.{DOMAIN}"), (
                f"{name} matches {host}, which resolves publicly — an unauthenticated route "
                "on a public hostname"
            )
        assert "authelia" not in router["middlewares"], name
        assert "rate-limit" in router["middlewares"], name


@pytest.mark.parametrize("containers", [None, TWO_SERVICES], ids=["inventory", "two"])
def test_a_probe_route_matches_one_exact_path_at_a_stated_priority(containers):
    """PathPrefix would widen an unauthenticated route to everything beneath it, and an
    unstated priority leaves the probe competing with the plain Host router on rule length —
    a tie that inverts silently the next time a hostname changes."""
    config = _dynamic_config(containers)
    for name, router in _probe_routers(config).items():
        assert "PathPrefix(" not in router["rule"], name
        assert router["rule"].count("Path(") == 1, name
        assert router.get("priority", 0) > 0, f"{name} states no priority"


def test_a_bypass_prefix_is_declared_with_a_reason():
    """A bypass router is public AND unauthenticated — the only routes here that are both. It
    exists because something automated calls the path without a session, which is a real
    reason, but it has to be a written one rather than a line of inventory nobody reviewed."""
    for svc in _bridged():
        for prefix in svc.get("bridge_bypass_prefixes", []):
            assert (svc["name"], prefix) in BRIDGE_BYPASS_PREFIXES, (
                f"{svc['name']} opens {prefix} publicly with no forward-auth and no entry in "
                "BRIDGE_BYPASS_PREFIXES explaining why it cannot be gated"
            )


def test_a_bypass_prefix_anchors_to_a_path_segment():
    """PathPrefix(`/ping`) also matches /pingXYZ, so the trailing slash is what keeps the hole
    the size it is meant to be. Carried across from the Docker label this replaces, where the
    same reasoning is written out."""
    for svc in _bridged():
        for prefix in svc.get("bridge_bypass_prefixes", []):
            assert prefix.startswith("/") and prefix.endswith("/"), (
                f"{svc['name']}'s bypass prefix {prefix!r} does not anchor to a path segment"
            )


def test_a_bypass_router_serves_the_hostname_it_names():
    """Same 421 exposure as the main routers: a bypass reuses the per-hostname service, so
    pointing the public router at the LAN transport would send a mismatched SNI."""
    config = _dynamic_config()
    services = config["http"]["services"]
    transports = config["http"]["serversTransports"]

    for name, router in _bypass_routers(config).items():
        host = router["rule"].split("`")[1]
        backend = services[router["service"]]["loadBalancer"]
        assert transports[backend["serversTransport"]]["serverName"] == host, name
        assert "authelia" not in router["middlewares"], name
        assert router["priority"] > 0, name
