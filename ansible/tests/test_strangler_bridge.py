#!/usr/bin/env python3
"""End-state guards for the (fully retired) strangler bridge.

Timeline: the forward bridge died at BT4 (2026-08-10, slice-7-bridge-teardown.md); the
last residual — livesync's X-Sync-Token gate — left the Docker edge on 2026-08-12 for a
Secret-mounted file-provider config on the k8s edge (the readonly kubeconfig can read
CRDs but is RBAC-denied Secrets, so that is the token's cluster-safe home). The failure
modes this suite guards are now:

  * a `bridge_hostname` key reappearing -> renders nothing anywhere (the macro parameter
    is `unsuffixed_hostname`), so the service's pre-migration names silently 404
  * ANY bridge machinery reappearing at the Docker edge -> two edges claim one Host rule
  * the livesync gate losing one of its routers -> tokened sync 404s, or the token gate
    silently stops gating (a bare Host router would forward past the header check)
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
GATE = (
    ANSIBLE / "roles" / "k8s" / "traefik" / "templates" / "livesync-gate-secret.yaml.j2"
)
DOMAIN = "example.com"
VIP = "10.0.0.240"


def _containers(host: str) -> list[dict]:
    data = yaml.safe_load((HOST_VARS / f"{host}.yml").read_text()) or {}
    return data.get("containers_list") or []


def _docker_edge_config() -> dict:
    """The Docker traefik file-provider config as Ansible would render it."""
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


def _gate_config() -> dict:
    """The livesync token gate as rendered into the k8s traefik's file provider."""
    env = Environment(
        loader=FileSystemLoader(str(GATE.parent)),
        undefined=ChainableUndefined,
        keep_trailing_newline=True,
    )
    rendered = env.get_template(GATE.name).render(
        domain=DOMAIN,
        k8s_namespace="homelab",
        livesync_sync_token="TESTTOKEN",
    )
    manifest = yaml.safe_load(rendered)
    return yaml.safe_load(manifest["stringData"]["livesync-gate.yml"])


def test_no_bridge_hostname_key_survives_anywhere():
    """The key was renamed to `unsuffixed_hostname` at the teardown; a reintroduced
    `bridge_hostname` matches NOTHING and the service's pre-migration names silently 404."""
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


def test_docker_edge_renders_no_bridge_machinery():
    """The Docker edge carries NO bridge anything since the livesync gate moved to the
    cluster: no routers referencing bridge services, no generated services/transports,
    and no livesync routers (its names are the k8s file provider's now)."""
    config = _docker_edge_config()
    routers = config["http"].get("routers") or {}
    for name, router in routers.items():
        assert "-bridge-" not in router.get("service", ""), (
            f"router {name} references a bridge service — the bridge is fully retired"
        )
        assert "livesync" not in name, (
            f"router {name}: livesync routing belongs to the k8s edge's file provider"
        )
    assert "services" not in config["http"] or not any(
        "-bridge-" in s for s in config["http"]["services"]
    ), "generated bridge services reappeared at the Docker edge"
    assert not (config["http"].get("serversTransports") or {}), (
        "bridge serversTransports reappeared at the Docker edge"
    )


def test_gate_serves_every_token_router():
    """The gate must render: a token router and an Authelia'd /_utils carve-out per
    livesync name form, the LAN-only probe router, and homelab-mcp's bearer router — and
    every router must reach its service. Losing a token predicate would forward PAST the
    gate."""
    gate = _gate_config()
    routers = gate["http"]["routers"]
    services = gate["http"]["services"]
    expected = {
        "livesync-sync-public",
        "livesync-sync-local",
        "livesync-utils-public",
        "livesync-utils-local",
        "livesync-probe",
        "homelab-mcp-local",
    }
    assert set(routers) == expected, f"gate routers drifted: {sorted(routers)}"
    for name, router in routers.items():
        assert router["service"] in services, f"{name} references a missing service"
        if name.startswith("livesync-sync"):
            assert "Header(`X-Sync-Token`" in router["rule"], (
                f"{name} lost the token predicate — the gate would forward ungated"
            )
        if name.startswith("livesync-utils"):
            assert any("authelia" in m for m in router["middlewares"]), (
                f"{name} lost its Authelia middleware"
            )
    # The probe stays LAN-only by construction: .local host, no public sibling.
    assert ".local." in routers["livesync-probe"]["rule"]
    assert f"Host(`livesync.{DOMAIN}`)" not in routers["livesync-probe"]["rule"]
    # mcp: LAN-only AND bearer-gated — either predicate alone is a regression.
    mcp_rule = routers["homelab-mcp-local"]["rule"]
    assert f"Host(`mcp.local.{DOMAIN}`)" in mcp_rule
    assert "Header(`Authorization`" in mcp_rule, (
        "homelab-mcp-local lost the bearer predicate — the gate would forward ungated"
    )
    assert f"Host(`mcp.{DOMAIN}`)" not in mcp_rule


def test_gate_stays_out_of_crd_objects():
    """The token's whole safety argument: it lives in a Secret (RBAC-denied to the
    readonly kubeconfig), never in an IngressRoute. A livesync IngressRoute matching the
    unsuffixed names would both leak routing back to CRDs and contest the gate's rules."""
    livesync_route = (
        ANSIBLE / "roles" / "k8s" / "livesync" / "templates" / "ingressroute.yaml.j2"
    ).read_text()
    assert "unsuffixed_hostname" not in [
        line
        for line in livesync_route.splitlines()
        if not line.lstrip().startswith(("#", "{#"))
        and "unsuffixed" in line
        and "=" in line
    ], (
        "livesync's IngressRoute must not claim the unsuffixed names — the gate owns them"
    )
    entry = next(c for c in _containers("daniel-box") if c["name"] == "livesync")
    assert "unsuffixed_hostname" not in entry, (
        "livesync's inventory entry must not set unsuffixed_hostname — the gate owns "
        "the unsuffixed names"
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
    assert "custom_edge" not in template, (
        "the reverse bridge's livesync carve-out is retired with the gate move"
    )


def test_every_unsuffixed_hostname_belongs_to_a_routed_service():
    """An unsuffixed_hostname on an entry with no hostname/port renders no IngressRoute at
    all — the name would resolve (wildcard -> VIP) and then 404 with nothing behind it."""
    for entry in _containers("daniel-box"):
        if "unsuffixed_hostname" in entry:
            assert entry.get("hostname") and entry.get("port"), (
                f"{entry['name']}: unsuffixed_hostname without a routed hostname/port"
            )
