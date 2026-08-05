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
    {"name": "alpha", "bridge_hostname": "alpha", "use_authelia": True},
    {"name": "beta", "bridge_hostname": "www", "use_authelia": False},
    {"name": "unbridged", "use_authelia": True},
]


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
        "alpha-bridge-1",
        "alpha-bridge-2",
        "beta-bridge-1",
        "beta-bridge-2",
    }
    hosts = sorted(r["rule"].split("`")[1] for r in routers.values())
    assert hosts == [
        f"alpha.{DOMAIN}",
        f"alpha.local.{DOMAIN}",
        f"www.{DOMAIN}",
        f"www.local.{DOMAIN}",
    ]
    # use_authelia is per service, not per bridge: beta is public by design.
    assert "authelia" in routers["alpha-bridge-1"]["middlewares"]
    assert "authelia" not in routers["beta-bridge-1"]["middlewares"]


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
        assert "authelia" in router["middlewares"], (
            f"{name} is the only gate for a use_authelia service and has no forward-auth"
        )
