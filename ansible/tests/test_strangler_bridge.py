#!/usr/bin/env python3
"""End-state guards for the (torn-down) strangler bridge — slice-7-bridge-teardown.md.

The forward bridge is gone (BT4, 2026-08-10): the cluster edge serves every migrated
service's unsuffixed names natively, the LAN wildcard answers the VIP, and daniel-server's
Traefik keeps only the CUSTOM-ROUTER residual (livesync's X-Sync-Token gate, whose secret
must stay in the file provider). Slice-2's suite guarded the bridge's existence; this one
guards its absence — the failure modes are now:

  * a `bridge_hostname` key reappearing -> renders nothing anywhere (the macro parameter is
    `unsuffixed_hostname`), so the service's pre-migration names silently 404
  * a generic bridge router reappearing at the Docker edge -> two edges claim one Host rule
  * the livesync residual losing its generated services/transports -> its hand-written
    routers dangle and phone sync dies
  * a reverse-bridge host losing one of its two name forms -> the LAN wildcard sends
    `.local` traffic to a router that isn't there (or SNI != Host and the Docker edge 421s)

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


def test_no_bridge_hostname_key_survives_anywhere():
    """The key was renamed to `unsuffixed_hostname` at the teardown; a reintroduced
    `bridge_hostname` matches NOTHING (macro, config, reverse bridge) and the service's
    pre-migration names silently 404."""
    for host_file in HOST_VARS.glob("*.yml"):
        for entry in yaml.safe_load(host_file.read_text()).get("containers_list") or []:
            assert "bridge_hostname" not in entry, (
                f"{host_file.name}: {entry.get('name')} declares bridge_hostname — "
                "the teardown renamed this to unsuffixed_hostname"
            )
    # Templates too, not just inventory: a selectattr('bridge_hostname', ...) in Jinja
    # matches zero entries and renders an EMPTY block instead of failing — exactly how
    # homepage's extra_hosts pins silently vanished at the teardown. Comments are exempt
    # (history is allowed to name the old key); expressions are not.
    ansible_root = HOST_VARS.parent.parent
    for tmpl in ansible_root.rglob("*.j2"):
        if "collections" in tmpl.parts:
            continue
        for i, line in enumerate(tmpl.read_text().splitlines(), 1):
            if "bridge_hostname" in line and not line.lstrip().startswith("#"):
                raise AssertionError(
                    f"{tmpl.relative_to(ansible_root)}:{i} references bridge_hostname "
                    "outside a comment — renamed to unsuffixed_hostname at the teardown"
                )


def test_docker_edge_renders_no_generic_bridge_routers():
    """Only the custom-router residual (livesync) may reference bridge services at the
    Docker edge; a generic bridge router reappearing would contest Host rules the cluster
    now owns."""
    config = _dynamic_config()
    custom = {
        c["name"] for c in _containers("daniel-box") if c.get("bridge_custom_routers")
    }
    for name, router in (config["http"].get("routers") or {}).items():
        service = router.get("service", "")
        if "-bridge-" in service:
            owner = service.split("-bridge-")[0]
            assert owner in custom, (
                f"router {name} references {service}: a generic forward-bridge router "
                "has reappeared at the Docker edge"
            )


def test_livesync_residual_stays_wired():
    """livesync's hand-written routers live at the Docker edge on purpose (the X-Sync-Token
    must stay out of CRDs); they reference GENERATED bridge services/transports, which must
    keep rendering for exactly the custom-router services."""
    config = _dynamic_config()
    routers = config["http"].get("routers") or {}
    services = config["http"].get("services") or {}
    transports = config["http"].get("serversTransports") or {}
    livesync_routers = [
        r for r in routers.values() if "livesync" in r.get("service", "")
    ]
    assert livesync_routers, "livesync's custom routers vanished from the Docker edge"
    for router in livesync_routers:
        svc = router["service"]
        assert svc in services, f"livesync router references missing service {svc}"
        transport = services[svc]["loadBalancer"]["serversTransport"]
        assert transport in transports, (
            f"{svc} references missing transport {transport}"
        )


def test_reverse_bridge_serves_both_name_forms_per_host():
    """Each Docker-hosted name needs a public AND a `.local` route (the LAN wildcard
    answers the VIP since BT3), and each form its own transport — SNI must equal Host or
    the Docker edge answers 421 (verified live when the bridges were built)."""
    template = (
        ANSIBLE / "roles" / "k8s" / "traefik" / "templates" / "reverse-bridge.yaml.j2"
    ).read_text()
    assert "Host(`{{ host }}.{{ domain }}`)" in template
    assert "Host(`{{ host }}.local.{{ domain }}`)" in template
    assert "reverse-bridge-{{ host }}-local" in template
    assert 'serverName: "{{ host }}.local.{{ domain }}"' in template


def test_every_unsuffixed_hostname_belongs_to_a_routed_service():
    """An unsuffixed_hostname on an entry with no hostname/port renders no IngressRoute at
    all — the name would resolve (wildcard -> VIP) and then 404 with nothing behind it."""
    for entry in _containers("daniel-box"):
        if "unsuffixed_hostname" in entry:
            assert entry.get("hostname") and entry.get("port"), (
                f"{entry['name']}: unsuffixed_hostname without a routed hostname/port"
            )
