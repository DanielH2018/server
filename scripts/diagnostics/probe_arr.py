"""`probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
"""

import json

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core
from probe_core import config_get, ha_curl_argv
from probe_health import resolve_service_ip

# Sonarr/Radarr speak /api/v3, Prowlarr /api/v1. The X-Api-Key comes from SOPS
# and is fed to curl via stdin (arr_curl_config), never argv — same guard as ha.
#
# NB this deliberately does NOT go through k8s_endpoint (Traefik + Authelia), unlike
# scrutiny/prometheus/loki. Confirmed live 2026-08-17: sonarr has no Authelia
# access_control bypass rule for its /api/* paths (scrutiny does — config-secret.yaml.j2),
# so a Traefik-routed GET 302s to the Authelia login page instead of reaching sonarr. The
# apps' own configarr/janitorr configs (config.yml.j2, application.yml.j2) hit
# `http://sonarr:8989` directly — the in-cluster Service DNS name — for the same reason.
# arr_url() therefore keeps the pre-migration ip:port shape; only the IP source changed,
# from `docker inspect` to the Service's ClusterIP (resolve_arr_ip, k8s's equivalent of a
# stable container IP — a Service's ClusterIP does not change across pod restarts/redeploys).
ARR_PORTS = {"sonarr": 8989, "radarr": 7878, "prowlarr": 9696}
ARR_API_VERSION = {"sonarr": "v3", "radarr": "v3", "prowlarr": "v1"}


def arr_url(ip, app, path):
    """Build an *arr API URL.

    Normalizes a leading `/`, an `api/` prefix, and a redundant version segment so `health`,
    `/health`, `api/v3/health`, and `v3/health` all resolve to the app's correct
    `/api/<ver>/health`.
    """
    ver = ARR_API_VERSION[app]
    p = path.lstrip("/")
    if p.startswith("api/"):
        p = p[len("api/") :]
    if p.startswith(ver + "/"):
        p = p[len(ver) + 1 :]
    return f"http://{ip}:{ARR_PORTS[app]}/api/{ver}/{p}"


def arr_curl_config(api_key):
    """The `curl --config -` body carrying the *arr X-Api-Key header (via stdin)."""
    return f'header = "X-Api-Key: {api_key}"\n'


def resolve_arr_ip(app):
    """Resolve the *arr app's k8s Service ClusterIP.

    resolve_ip's k8s equivalent, used instead of k8s_endpoint because sonarr/radarr/prowlarr
    have no Authelia bypass rule for /api/* (see the comment above ARR_PORTS). A ClusterIP is
    stable across pod restarts and redeploys, so this doesn't reintroduce the hand-copied-IP
    staleness `docker inspect` was resolving around in the first place.

    CAVEAT confirmed live 2026-08-17: this only reaches the app when its pod is scheduled on
    THIS node (daniel-box). Each app's NetworkPolicy allows ingress only from specific pod
    selectors, no ipBlock for the host — sonarr/radarr (on daniel-box) answered anyway, but
    prowlarr (on daniel-server that day) refused the connection although ICMP to its pod IP
    got through, so this is the NetworkPolicy's enforcement, not routing. Host-originated
    traffic apparently doesn't pass through the destination node's own NetworkPolicy iptables
    the same way same-node traffic does. This will flip on the next reschedule; a real fix
    needs a NetworkPolicy ipBlock for the node (ansible/roles/k8s/*/templates/), out of scope
    here.
    """
    return resolve_service_ip(app)


def run_arr(ns):
    """Read-only *arr API GET, resolved to the app's k8s Service ClusterIP.

    sonarr/radarr/prowlarr have run as k8s Deployments since 2026-08-07 (B4c) and have
    no Docker container left to `docker inspect` an IP from — this used to shell out to
    `resolve_ip(ns.app)`, which died with `FileNotFoundError: 'docker'` on both cluster
    nodes. resolve_arr_ip replaces it with the same idea (resolve the current address at
    run time) via kubectl instead of docker — see the comment above ARR_PORTS for why
    this talks to the Service directly instead of going through k8s_endpoint like every
    other cluster subcommand. Pulls <app>_api_key from SOPS and passes it via stdin.
    Pretty-prints JSON by default; `--json` prints the raw response.
    """
    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(arr_url("<arr-clusterip>", ns.app, ns.path)))
            + "   # + X-Api-Key: <redacted> (via --config stdin)"
        )
        return 0
    url = arr_url(resolve_arr_ip(ns.app), ns.app, ns.path)
    body = config_get(url, arr_curl_config(core.sops_extract(f"{ns.app}_api_key")))
    if ns.json:
        print(body, end="")
        return 0
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body.strip())
        return 1
    return 0
