#!/usr/bin/env python3
"""End-state guards for the retired strangler bridge.

livesync's X-Sync-Token gate lives in a Secret-mounted file-provider config on the k8s edge
(the readonly kubeconfig can read CRDs but is RBAC-denied Secrets, so that is the token's
cluster-safe home). The failure modes this suite guards are:

  * the livesync gate losing one of its routers -> tokened sync 404s, or the token gate
    silently stops gating (a bare Host router would forward past the header check)
  * the authelia entry losing the unsuffixed `auth` name -> the OIDC issuer Jellyfin's SSO
    plugin points at stops resolving to the portal

Run: uv run pytest tests/services/test_strangler_bridge.py
"""

from lib.k8s_roles import resolved_manifest_files
from lib import yaml_fast
from jinja2 import ChainableUndefined
from lib.guard_jinja_env import template_env
from _helpers import K8S_ROLES, SHARED_TPL
from lib.render_guard import k8s_services
from lib.repo_paths import ALL_VARS


GATE = K8S_ROLES / "traefik" / "templates" / "livesync-gate-secret.yaml.j2"
DOMAIN = "example.com"


def _containers() -> list[dict]:
    return k8s_services()


def _gate_config() -> dict:
    """The livesync token gate as rendered into the k8s traefik's file provider."""
    env = template_env(GATE.parent, undefined_cls=ChainableUndefined)
    rendered = env.get_template(GATE.name).render(
        domain=DOMAIN,
        k8s_namespace="homelab",
        livesync_sync_token="TESTTOKEN",
    )
    manifest = yaml_fast.safe_load(rendered)
    return yaml_fast.safe_load(manifest["stringData"]["livesync-gate.yml"])


def test_gate_serves_every_token_router():
    """The gate must serve every token router, and every router must reach its service.

    A token router and an Authelia'd /_utils carve-out per livesync name form, the LAN-only probe
    router, and homelab-mcp's bearer router. Losing a token predicate would forward PAST the gate.
    """
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
        "homelab-mcp-probe",
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
    # The mcp probe router is the ONE ungated path through this gate, so the two things that
    # keep it harmless are asserted rather than assumed: an exact Path (not a PathPrefix, which
    # would open /health-anything) and LAN-only. It exists because a tile probing
    # /mcp would accept the edge's own 404 as health and so could not see a total-404 edge.
    probe_rule = routers["homelab-mcp-probe"]["rule"]
    assert "Path(`/health`)" in probe_rule
    assert "PathPrefix" not in probe_rule, (
        "homelab-mcp-probe must match one exact path — a prefix opens more than /health"
    )
    assert f"Host(`mcp.local.{DOMAIN}`)" in probe_rule
    assert f"Host(`mcp.{DOMAIN}`)" not in probe_rule


def test_gate_stays_out_of_crd_objects():
    """The token's whole safety argument: it lives in a Secret, never in an IngressRoute.

    That Secret is RBAC-denied to the readonly kubeconfig.

    livesync therefore renders NO IngressRoute at all: a bare `Host(livesync…)` router beside the
    gate's would break the gate's invariant — that a request without the token matches no router
    and 404s before CouchDB. Re-adding the template puts that hole back.
    """
    assert not (
        K8S_ROLES / "livesync" / "templates" / "ingressroute.yaml.j2"
    ).exists(), "livesync must render no IngressRoute — the token gate owns its names"
    files, _secret = resolved_manifest_files("livesync")
    assert "ingressroute.yaml" not in files, (
        "livesync's containers_list entry carries a hostname, which earns the shared route "
        "unless its include excludes it"
    )


def test_the_k8s_portal_owns_the_unsuffixed_auth_name():
    authelia = next(e for e in _containers() if e["name"] == "authelia")
    assert authelia.get("hostname") == "auth", (
        "the k8s portal must own the unsuffixed auth name — it is the OIDC issuer "
        "Jellyfin's SSO plugin points at"
    )


def test_dual_hostname_machinery_stays_retired():
    """Every k8s service answers on exactly ONE name.

    The `unsuffixed_hostname` key, the `k8s_hostname_suffix` var and the macro's second route
    are retired. Reintroducing any of them puts two routers on one service.
    """
    for entry in _containers():
        assert "unsuffixed_hostname" not in entry, (
            f"{entry['name']}: unsuffixed_hostname is retired — `hostname` is the only name"
        )
        assert not (entry.get("hostname") or "").endswith("-k8s"), (
            f"{entry['name']}: the -k8s hostname suffix is retired"
        )
    all_vars = ALL_VARS.read_text()
    assert "\nk8s_hostname_suffix:" not in all_vars
    macro = (SHARED_TPL / "ingressroute.yml.j2").read_text()
    assert "unsuffixed_hostname" not in macro
