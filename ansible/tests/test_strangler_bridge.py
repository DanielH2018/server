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
    ("karakeep", "/api/"): (
        "Reproduces the karakeep-api Docker router, public since it was written: /api/v1 is "
        "Bearer-token authenticated and the browser extension and mobile app cannot pass 2FA. "
        "Measured unauthenticated: /api/v1/users/me and /api/v1/bookmarks both 401, so "
        "karakeep's own token check is the gate. This carries an existing hole across rather "
        "than opening one."
    ),
}


# LAN-only API paths another homelab service calls directly, keyed by (service, prefix). Same
# declare-with-a-reason rule as BRIDGE_BYPASS_PREFIXES; these are narrower (never public) but
# they are still routes that skip forward-auth.
BRIDGE_LAN_PREFIXES = {
    ("freshrss", "/api/greader.php/"): (
        "Homepage's FreshRSS widget calls the GReader API for unread counts. It sends a "
        "username and password, and the API answers 401 without them — measured — so "
        "FreshRSS's own auth is the gate, not this route."
    ),
    ("speedtest", "/api/"): (
        "Homepage's speedtest widget reads /api/speedtest/latest. Unlike FreshRSS it passes no "
        "credentials, so this really is unauthenticated: any LAN host can read the speed-test "
        "history. Accepted deliberately — the data is low-sensitivity and it is never public."
    ),
}


def _lan_routers(config: dict) -> dict[str, dict]:
    return {
        name: router
        for name, router in config["http"]["routers"].items()
        if "-bridge-lan-" in name
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
    """Every router pointing at a bridge service, selected by TARGET rather than by its own name.

    Selecting on the router's name would have let livesync's four hand-written routers
    (livesync-sync-*, livesync-utils-*) escape every invariant below — and they need them most:
    they cross the same HTTPS-to-the-VIP transport, so the 421 trap applies to them exactly as it
    does to a generated one. Anything that forwards to the cluster is checked here, whoever wrote
    it.
    """
    return {
        name: router
        for name, router in config["http"]["routers"].items()
        if "-bridge-" in router.get("service", "")
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
    router matching two hostnames can only ever get one of them right.

    Exactly one Host() IS the invariant. This used to also reject any `||` in the rule, which was
    a proxy for the same thing and too broad: livesync's /_utils router alternates two PATH
    matchers, which cannot disagree with an SNI. The Host count catches `Host(a) || Host(b)`
    on its own."""
    config = _dynamic_config(containers)
    for name, router in _bridge_routers(config).items():
        rule = router["rule"]
        assert rule.count("Host(") == 1, (
            f"{name} matches more than one hostname; each needs its own router and "
            "serversTransport or one of them answers 421"
        )


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
        # From the TARGET, not the router name — a hand-written router is not named after the
        # service it bridges (livesync-sync-public -> livesync-bridge-public).
        service = router["service"].rsplit("-bridge-", 1)[0]
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
        # And likewise for a LAN API route, earned by
        # test_a_lan_prefix_is_declared_and_never_public.
        if name in _lan_routers(config):
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


def test_a_lan_prefix_is_declared_and_never_public():
    """A LAN API route skips forward-auth like a bypass, but for a caller inside the homelab
    rather than a public one. Both halves are load-bearing: declared with a reason, and never
    reachable from the internet. Losing the second turns an app-auth-protected endpoint into a
    public one, which for speedtest — which passes no credentials at all — would be an open API.
    """
    config = _dynamic_config()
    for svc in _bridged():
        for prefix in svc.get("bridge_lan_prefixes", []):
            assert (svc["name"], prefix) in BRIDGE_LAN_PREFIXES, (
                f"{svc['name']} opens {prefix} without forward-auth and has no entry in "
                "BRIDGE_LAN_PREFIXES explaining what does protect it"
            )
            assert prefix.startswith("/") and prefix.endswith("/"), (
                f"{svc['name']}'s LAN prefix {prefix!r} does not anchor to a path segment"
            )

    for name, router in _lan_routers(config).items():
        host = router["rule"].split("`")[1]
        assert host.endswith(f".local.{DOMAIN}"), (
            f"{name} matches {host}, which resolves publicly — this route has no forward-auth"
        )
        assert "authelia" not in router["middlewares"], name


# --- Backup verification must follow the service across the migration --------------------
#
# A migrated service's data leaves daniel-server's disk, but the directory it left behind does
# not: Kopia keeps backing up a frozen copy, so any check asserting on that path keeps passing.
# freshrss sat in both Docker-side lists for a day after its 2026-08-05 cutover, reporting green
# for data that had stopped changing. That is worse than an outright gap, because it looks like
# coverage. Second occurrence of the same class as the homepage-widget regression — check what
# depends on a service, not just what it depends on — so it is a test rather than another note.

MONITOR_BRIDGE_CHECK = (
    ANSIBLE / "roles" / "containers" / "monitor-bridge" / "files" / "check.py"
)
RESTORE_DRILL = (
    ANSIBLE / "roles" / "containers" / "kopia" / "files" / "restore-drill.sh"
)


def _migrated_service_names() -> set[str]:
    """Services that RETIRED from daniel-server, not merely ones that also exist in the cluster.

    The difference is the whole test. `traefik` and `authelia` run in both places during
    coexistence — daniel-server's Docker copies are still the public edge and still gate the
    bridged services — so their Kopia sentinels point at files that very much still change.
    Only a service absent from daniel-server's containers_list has left a frozen directory
    behind, and only then does a Docker-side backup assertion become a lie.
    """
    in_cluster = {
        c["name"] for c in _containers("daniel-box") if c.get("platform") == "k8s"
    }
    still_on_server = {c["name"] for c in _containers("daniel-server")}
    return in_cluster - still_on_server


def _kopia_sentinel_services() -> set[str]:
    import re

    block = re.search(
        r"_DEFAULT_BACKUP_SENTINELS = \[(.*?)\n\]",
        MONITOR_BRIDGE_CHECK.read_text(),
        re.S,
    )
    assert block, "could not find _DEFAULT_BACKUP_SENTINELS — regex drift?"
    return {m.group(1) for m in re.finditer(r'"([^"/]+)/', block.group(1))}


def _restore_drill_services() -> set[str]:
    import re

    block = re.search(r"^SVCS=\((.*?)\)", RESTORE_DRILL.read_text(), re.M | re.S)
    assert block, "could not find the SVCS array — regex drift?"
    return set(block.group(1).split())


@pytest.mark.parametrize(
    "reader",
    [_kopia_sentinel_services, _restore_drill_services],
    ids=["monitor-bridge-sentinels", "kopia-restore-drill"],
)
def test_a_migrated_service_is_not_still_verified_on_the_docker_side(reader):
    """Once a service runs in k3s, its data is a Longhorn PVC and Longhorn's per-volume check
    owns proving it restorable. Leaving it in a Kopia-side list means that list is asserting on
    an abandoned directory — it passes forever, and it passes for the wrong reason."""
    stale = _migrated_service_names() & reader()

    assert not stale, (
        f"{', '.join(sorted(stale))} migrated to k3s but is still verified against "
        "containers/ on daniel-server, where the data no longer changes. Drop it from that "
        "list — longhorn-backup-health.sh asserts per-volume freshness cluster-side."
    )


def test_a_bridge_that_suppresses_its_router_is_still_reachable():
    """bridge_custom_routers turns off the generated Host router so hand-written ones can gate
    the traffic. The generated SERVICE and serversTransport remain, and if nothing references
    them the service is simply unreachable — a 404 on the real hostname, with every other guard
    here still green because they only check routers that exist.

    Both hostnames must be covered, not just one. Writing the public router and forgetting the
    LAN one is the natural mistake, and it fails only for in-homelab callers.
    """
    config = _dynamic_config()
    routers = config["http"]["routers"]

    for svc in _bridged():
        if not svc.get("bridge_custom_routers"):
            continue
        for key in ("public", "local"):
            generated = f"{svc['name']}-bridge-{key}"
            assert generated not in routers, (
                f"{svc['name']} sets bridge_custom_routers but {generated} was still generated"
            )
            assert any(r.get("service") == generated for r in routers.values()), (
                f"{svc['name']} suppressed its generated router and nothing references "
                f"{generated}, so that hostname 404s"
            )


# --- A per-router rate limit has to survive the migration ---------------------------------
#
# A bridged request is metered twice: once by the Docker edge router, once by the cluster route
# it forwards to. The TIGHTER ceiling wins, so a service the Docker side deliberately exempted
# from the default limit is silently put back under it unless the bridge route is given the same
# exemption. livesync 429'd a phone mid-replication within the hour of cutover for exactly this:
# it passed the edge's 6000/min and then hit the cluster's 300/min.

K8S_DYNAMIC = ANSIBLE / "roles" / "k8s" / "traefik" / "templates" / "dynamic.yaml.j2"


def test_a_service_with_its_own_edge_rate_limit_keeps_it_across_the_bridge():
    """The Docker side names a per-service limiter `rate-limit-<service>`. If one exists for a
    bridged service, that service had a documented reason to be exempt from the default ceiling,
    and the reason does not stop applying because the workload moved."""
    config = _dynamic_config()
    edge_limiters = set(config["http"]["middlewares"])

    for svc in _bridged():
        if f"rate-limit-{svc['name']}" not in edge_limiters:
            continue
        override = svc.get("bridge_rate_limit")
        assert override and override != "rate-limit-bridge", (
            f"{svc['name']} has a per-router rate-limit-{svc['name']} at the Docker edge but "
            "its bridge route falls back to rate-limit-bridge, whose lower ceiling wins — the "
            "exemption is undone by the migration. Set bridge_rate_limit."
        )


def test_a_bridge_rate_limit_override_names_a_middleware_that_exists():
    """A typo here does not fail the deploy loudly: Traefik drops the route whose middleware it
    cannot resolve, so the service 404s on its real hostname instead."""
    import re

    declared = set(re.findall(r"^\s*name: (\S+)", K8S_DYNAMIC.read_text(), re.M))

    for svc in _bridged():
        override = svc.get("bridge_rate_limit")
        if not override:
            continue
        assert override in declared, (
            f"{svc['name']} routes through {override}, which no Middleware in "
            "roles/k8s/traefik/templates/dynamic.yaml.j2 defines"
        )
