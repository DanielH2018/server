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


def _dynamic_config() -> dict:
    """The traefik file-provider config as Ansible would render it on daniel-server."""
    env = Environment(
        loader=FileSystemLoader(str(TRAEFIK)),
        undefined=ChainableUndefined,
        keep_trailing_newline=True,
    )
    rendered = env.get_template("config.yml.j2").render(
        domain=DOMAIN,
        k3s_metallb_ingress_vip=VIP,
        hostvars={"daniel-box": {"containers_list": _containers("daniel-box")}},
    )
    return yaml.safe_load(rendered)


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


def test_each_bridge_router_matches_exactly_one_host():
    """The 421 trap. The backend is HTTPS, and Traefik rejects a request whose Host header
    disagrees with the SNI it sent upstream. serversTransport.serverName holds one value, so a
    router matching two hostnames can only ever get one of them right."""
    config = _dynamic_config()
    for name, router in _bridge_routers(config).items():
        rule = router["rule"]
        assert rule.count("Host(") == 1, (
            f"{name} matches more than one hostname; each needs its own router and "
            "serversTransport or one of them answers 421"
        )
        assert "||" not in rule, name


def test_each_bridge_sends_an_sni_matching_the_host_it_serves():
    config = _dynamic_config()
    services = config["http"]["services"]
    transports = config["http"]["serversTransports"]

    for name, router in _bridge_routers(config).items():
        host = router["rule"].split("`")[1]
        backend = services[router["service"]]["loadBalancer"]
        assert backend["servers"][0]["url"] == f"https://{VIP}"
        assert transports[backend["serversTransport"]]["serverName"] == host, (
            f"{name} serves {host} but sends a different SNI, which Traefik answers with 421"
        )


def test_an_authed_bridge_authenticates_at_the_docker_edge():
    """The k8s side of a bridged route deliberately carries no forward-auth, so this is the
    only place the user is authenticated. A bridge that lost it would publish the service."""
    config = _dynamic_config()
    authed = {c["name"] for c in _bridged() if c.get("use_authelia")}

    for name, router in _bridge_routers(config).items():
        service = name.rsplit("-bridge-", 1)[0]
        if service not in authed:
            continue
        assert "authelia" in router["middlewares"], (
            f"{name} is the only gate for a use_authelia service and has no forward-auth"
        )
