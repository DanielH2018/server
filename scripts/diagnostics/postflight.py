#!/usr/bin/env python3
"""Verify the post-deploy setup that Ansible can't do (ansible/README.md §9).

Every step in §9 lives in an app's own database, and most of them fail SILENTLY —
the container stays healthy while the feature behind it does nothing. This script
exercises each one against the live host and exits non-zero naming the README item
that still needs a human.

    uv run python scripts/diagnostics/postflight.py

Read-only. Credentials come from SOPS and are handed to curl via `--config -` on
stdin, never argv, so they never reach `ps` or shell history (same guard as probe.py).
A check whose container isn't deployed here reports SKIP, not a failure — the Pi runs
almost none of these.
"""

import json
import subprocess
import sys
from pathlib import Path as _Path

# `probe_lib` is a namespace package under `scripts/`, so reaching it by package name needs
# `scripts/` on sys.path: a directly-invoked script — which is how this one runs — gets only
# its own directory, and pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE
# the imports below. It used to be supplied as a side effect of `import probe` executing its
# own insert first, which made the order of these four lines load-bearing and unremarked.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from diagnostics.probe_lib import core
from diagnostics.probe_lib import arr
from diagnostics.probe_lib import health_docker
from diagnostics.probe_lib import ha
from diagnostics.probe_lib import monitors
from lib import k8s_roles

TIMEOUT = 10

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


class Skip(Exception):
    """This host doesn't run the service, so the README item doesn't apply to it."""


def get(url, header=None, timeout=TIMEOUT, resolve=None):
    """GET url, returning (http_status, body).

    `header` is a full `curl --config` body (e.g. `header = "X-Api-Key: ..."`) fed via stdin so
    credentials stay out of argv. `resolve` is a curl --resolve pin (core.k8s_endpoint's second
    element) for cluster routes the host shell can't resolve. status 0 means curl itself failed
    (connection refused, DNS, timeout).
    """
    argv = [
        "curl",
        "-sS",
        "--max-time",
        str(timeout),
        "-o",
        "-",
        "-w",
        "\n%{http_code}",
    ]
    if resolve:
        argv += ["--resolve", resolve]
    if header:
        argv += ["--config", "-"]
    argv.append(url)
    out = subprocess.run(argv, input=header or "", capture_output=True, text=True)
    if out.returncode != 0:
        return 0, out.stderr.strip()
    body, _, code = out.stdout.rpartition("\n")
    return int(code or 0), body


def service_ip(name):
    """The workload's k8s Service ClusterIP, or Skip if there is no Service to read.

    This resolved a Docker bridge IP until 2026-08-25, which meant every check reaching a
    workload directly — the three *arr keys, the jellyfin key, Authelia — had been dead on
    both cluster nodes since the 2026-08-14 Docker retirement, failing with
    `FileNotFoundError: 'docker'` rather than checking anything. probe.py's `arr` subcommand
    was fixed for exactly this on 2026-08-07; postflight kept the old resolver.
    """
    try:
        return health_docker.resolve_service_ip(name)
    except SystemExit as exc:
        raise Skip(str(exc)) from exc


def secret(name):
    try:
        value = core.sops_extract(name)
    except SystemExit as exc:
        return "", str(exc)
    return value, ""


def route_host(service):
    """`service`'s Traefik route hostname, read from containers_list.

    NOT the service name. Authelia answers on `auth` — that name is the OIDC issuer Jellyfin's
    SSO plugin points at, so it cannot be renamed to match the Service. Read from inventory
    rather than listed here, because a literal map would keep pinning a hostname that moved and
    the resulting `--resolve` would be silently wrong.
    """
    entry = k8s_roles.k8s_entries().get(service) or {}
    return entry.get("hostname") or service


def get_via_service(service, path, port, header=None):
    """GET `path` on `service`, ClusterIP first and its Traefik route as the fallback.

    The ClusterIP is one hop with no TLS and no edge, so it stays the fast path. It only answers
    a caller on the node the pod is scheduled on, though: each workload's NetworkPolicy admits
    specific pod selectors and no ipBlock for the node. postflight runs on daniel-box and
    nowhere else, so until this fallback existed every check below was structurally SKIP
    whenever its pod sat on daniel-server — a check that never runs rather than a false alarm
    (#1633). The route reaches either node, pinned to the MetalLB ingress VIP because this
    host's resolver does not answer `.local` names with the cluster edge.

    Returns (status, body). status 0 means curl failed on BOTH paths — then the pod really is
    unreachable from here. Raises Skip when there is no Service at all, which means the
    workload is not deployed on this cluster.
    """
    ip = service_ip(service)
    status, body = get(f"http://{ip}:{port}{path}", header)
    if status:
        return status, body
    base, pin = core.k8s_endpoint(route_host(service))
    return get(f"{base}{path}", header, resolve=pin)


def _forward_auth_intercepted(app, status):
    """A 3xx from the route is Authelia, not the app — so it says nothing about the credential.

    `use_authelia: true` puts the forward-auth middleware ahead of the backend, and the
    redirect fires there: the app never sees the request. Reporting it as `HTTP 302 — the key
    doesn't match` would send someone to rotate a key that is fine, which is strictly worse
    than the SKIP this replaces. Keyed on the response rather than on the inventory flag, so it
    still holds if a service's `use_authelia` is flipped later.

    The *arr monitoring routes carry no forward-auth and admit daniel-box as well as
    daniel-server since #1642, so a 3xx from one of those three now means the request missed
    that route's PathPrefix and fell through to the app's own Authelia'd route — check
    `ARR_MONITORED_PATH` against the role's `ingressroute-monitoring.yaml.j2`.
    """
    return SKIP, (
        f"{app}'s route answered HTTP {status} — Authelia forward-auth intercepted it, so the "
        f"app never saw the request; credential unverifiable from this host"
    )


# §9.1 + §9.2: Uptime-Kuma
# Both are checked through Prometheus rather than Kuma itself: Kuma 2.x drives its
# admin wizard and API-key minting over Socket.IO only, so there is no REST route to
# ask "does an admin exist". The scrape is the observable consequence of both steps.


def _cluster_prom_query(promql):
    """Query the cluster Prometheus through its LAN query route.

    The uptime-kuma job moved there at the Phase D dashboard triage, PG1. VIP-pinned —
    this host's resolver bypasses the LAN DNS, so the name alone does not reach the
    cluster edge.
    """
    base, pin = core.k8s_endpoint("prometheus")
    from urllib.parse import urlencode

    url = f"{base}/api/v1/query?" + urlencode({"query": promql})
    return get(url, resolve=pin)


def check_kuma_monitors():
    """§9.1 — no admin means AutoKuma provisions zero monitors, so nothing is watched."""
    status, body = _cluster_prom_query("count(monitor_status)")
    if status != 200:
        return FAIL, f"cluster prometheus query returned {status}"
    result = json.loads(body).get("data", {}).get("result", [])
    if not result:
        return FAIL, "AutoKuma has provisioned 0 monitors — create the Kuma admin"
    count = int(float(result[0]["value"][1]))
    return OK, f"{count} monitors provisioned"


def check_kuma_drift():
    """§9.1 — a monitor that is declared and never created reads as green everywhere else.

    The check above counts what the exporter emits, which is also the denominator, so a tile
    that vanishes cannot move it. `probe.py kuma-drift` compares that set against the
    declaration file instead; see its docstring for the 2026-08-20 instance and for why a push
    monitor inside its own interval after a Kuma restart is PENDING rather than missing.
    """
    status, body = _cluster_prom_query('monitor_status{job="uptime-kuma"}')
    if status != 200:
        return FAIL, f"cluster prometheus query returned {status}"
    live = {
        (s.get("metric") or {}).get("monitor_name")
        for s in json.loads(body).get("data", {}).get("result", [])
    }
    live.discard(None)
    # These four names live in `probe_lib.monitors`, not in `probe.py` — reading them off
    # `probe` raised AttributeError and the section reported FAIL, which reads as drift found
    # rather than as a check that never ran (#1562).
    with open(monitors.STATIC_MONITORS_PATH) as f:
        declared = monitors.parse_declared_monitors(f.read())
    # gate_states is not optional here. Passing none excuses EVERY gated monitor whatever its
    # secret says, which is what this section did until 2026-09-10: all seven gated monitors
    # read "gated on <var>, which could not be read" while the line said [OK] (#1632). A gated
    # monitor is the one nothing else watches, so the drift half could not see the case it
    # exists for. resolve_gate_states is `probe.py kuma-drift`'s own constructor — shared, so a
    # caller cannot omit it by forgetting it.
    text, code = monitors.format_kuma_drift(
        declared,
        live,
        monitors.kuma_pod_age_seconds(),
        gate_states=monitors.resolve_gate_states(declared, live),
    )
    return (FAIL if code else OK), text.replace("\n", "; ").strip()


def check_kuma_scrape():
    """§9.2 — a stale prometheus_kuma_api_key leaves the uptime-kuma target at 401.

    Reads `up{job=...}` rather than the targets API: the cluster route only admits /api/v1/query
    paths, and up==0 is the same evidence the target listing gave.
    """
    status, body = _cluster_prom_query('up{job="uptime-kuma"}')
    if status != 200:
        return FAIL, f"cluster prometheus query returned {status}"
    result = json.loads(body).get("data", {}).get("result", [])
    if not result:
        return SKIP, "no uptime-kuma scrape target configured"
    if float(result[0]["value"][1]) == 1:
        return OK, "uptime-kuma scrape target up"
    return FAIL, "uptime-kuma target down (stale prometheus_kuma_api_key?)"


# §9.3: *arr + jellyfin API keys
# A fresh *arr generates its own random key on first start, so the SOPS value that
# configarr / janitorr / homepage / monitor-bridge / autofix-bridge authenticate with
# is wrong until it's pasted in. Every consumer then 401s against a healthy service.


def _unreachable(app, detail):
    """A ClusterIP that does not answer the host is a placement fact, not a bad credential.

    `get()` returns status 0 when curl itself failed. Reporting that as "the key doesn't
    match" sends someone to rotate a key that is fine. Each *arr's NetworkPolicy admits
    specific pod selectors and no ipBlock for the node, so a host-originated GET only
    reaches an app scheduled on THIS node — confirmed 2026-08-17 and again 2026-08-25,
    both times with prowlarr on daniel-server while sonarr and radarr answered.

    Since #1633 this is the LAST resort rather than the first: `get_via_service` tries the
    service's Traefik route before a caller gets here, so reaching this means neither the
    ClusterIP nor the edge answered.
    """
    return SKIP, f"{app} unreachable from this host (pod on another node?) — {detail}"


# The path each *arr's `-monitoring` IngressRoute admits, which is the only path the route
# half of `get_via_service` can reach. It read `/api/<ver>/system/status` until 2026-09-10 and
# that path is on no route's PathPrefix, so the fallback 302'd into Authelia and the check was
# structurally SKIP whenever the pod sat on the other node (#1642). A 200 here with the SOPS
# key proves the credential just as well as system/status did: both are authenticated reads.
#
# Kept in step with the three `ingressroute-monitoring.yaml.j2` templates by
# ansible/tests/k8s/test_arr_monitoring_routes_admit_postflight.py — a prefix edited on one
# side alone puts this check back where #1642 found it.
ARR_MONITORED_PATH = {
    "sonarr": "/api/v3/queue",
    "radarr": "/api/v3/queue",
    "prowlarr": "/api/v1/indexer",
}


def check_arr_key(app):
    """§9.3 — verify ``app``'s SOPS-held API key authenticates against its own instance.

    Args:
        app: The *arr app name (``sonarr``, ``radarr`` or ``prowlarr``), used both as the
            secret name prefix and to select the path in ``ARR_MONITORED_PATH``.
    """
    key, err = secret(f"{app}_api_key")
    if not key:
        return FAIL, err
    status, body = get_via_service(
        app,
        ARR_MONITORED_PATH[app],
        arr.ARR_PORTS[app],
        arr.arr_curl_config(key),
    )
    if status == 200:
        return OK, f"{app}_api_key authenticates"
    if status == 0:
        return _unreachable(app, body or "curl failed")
    if 300 <= status < 400:
        return _forward_auth_intercepted(app, status)
    return FAIL, f"HTTP {status} — {app}_api_key doesn't match the app's own key"


def check_jellyfin_key():
    """§9.3 — verify the SOPS-held ``jellyfin_api_key`` authenticates against Jellyfin."""
    key, err = secret("jellyfin_api_key")
    if not key:
        return FAIL, err
    status, body = get_via_service(
        "jellyfin", "/System/Info", 8096, f'header = "X-Emby-Token: {key}"\n'
    )
    if status == 200:
        return OK, "jellyfin_api_key authenticates"
    if status == 0:
        return _unreachable("jellyfin", body or "curl failed")
    if 300 <= status < 400:
        return _forward_auth_intercepted("jellyfin", status)
    return FAIL, f"HTTP {status} — mint the key in Jellyfin and sops set it"


# §9.4: Home Assistant long-lived tokens
# Four separate consumers each hold their own token; one bad token silently disables
# just that consumer, so they're checked individually rather than as a group.

HA_TOKENS = [
    "monitor_bridge_ha_token",
    "homepage_ha_token",
    "prometheus_ha_token",
    "claude_ha_token",
]


def check_ha_token(name):
    """§9.4 — verify one Home Assistant long-lived token still authenticates.

    Args:
        name: The SOPS secret name of the token to check (one of ``HA_TOKENS``).
    """
    # Since slice-5 B3 HA runs in the cluster — core.ha_base() is the bridge URL, the same
    # endpoint before and after the cutover (no container to inspect).
    token, err = secret(name)
    if not token:
        return FAIL, err
    status, _ = get(ha.ha_get_url(core.ha_base(), ""), ha.ha_curl_config(token))
    if status == 200:
        return OK, "token accepted"
    return FAIL, f"HTTP {status} — re-mint under Profile → Security"


# §9.5: Authelia
# The role asserts the OIDC material exists, so a missing secret fails the deploy
# rather than failing silently. What it can't tell you is whether the running
# instance actually came up with it.


def check_authelia():
    """§9.5 — verify Authelia is serving and its OIDC secrets are present.

    The OIDC material is read from SOPS first, so it is still checked even when the network
    half cannot run: it needs no network at all.

    The reachability half goes through `get_via_service`, so the portal is asked on either
    node. Its own IngressRoute carries no forward-auth — gating the login page behind the
    login page is a redirect loop — so `/api/health` on `auth.local.<domain>` reaches the
    backend and returns Authelia's own `{"status":"OK"}`. Measured from daniel-box on
    2026-09-10 with the pod on daniel-server: HTTP 200.

    A status of 0 means curl itself failed on both paths, which is the placement fact
    `_unreachable` describes for the *arr checks — not an outage. Reporting it as
    "Authelia is not serving" was a false alarm on the fleet's most load-bearing service
    whenever the run was on the node Authelia is not on (#1564).
    """
    missing = [
        name
        for name in (
            "authelia_oidc_hmac_secret",
            "authelia_client_password_hash",
            "authelia_oidc_rsa_key_content",
        )
        if not secret(name)[0]
    ]
    if missing:
        return FAIL, "missing OIDC material: " + ", ".join(missing)
    status, body = get_via_service("authelia", "/api/health", 9091)
    if status == 0:
        return _unreachable("authelia", body or "curl failed")
    if 300 <= status < 400:
        return _forward_auth_intercepted("authelia", status)
    if status != 200:
        return FAIL, f"HTTP {status} — Authelia is not serving"
    return OK, f"healthy ({json.loads(body).get('status', '?')}), OIDC material present"


# §9.6 was Portainer environments, removed with Portainer itself on 2026-08-09
# (slice-7 Phase B). The numbering below is left alone so §9.x keeps matching
# ansible/README.md.

CHECKS = [
    ("9.1", "Uptime-Kuma admin", check_kuma_monitors),
    ("9.1", "Uptime-Kuma monitor drift", check_kuma_drift),
    ("9.2", "Kuma API key (prometheus scrape)", check_kuma_scrape),
    *[
        ("9.3", f"{app} API key", (lambda a: lambda: check_arr_key(a))(app))
        for app in ("sonarr", "radarr", "prowlarr")
    ],
    ("9.3", "jellyfin API key", check_jellyfin_key),
    *[("9.4", name, (lambda n: lambda: check_ha_token(n))(name)) for name in HA_TOKENS],
    ("9.5", "Authelia", check_authelia),
]


def main():
    """Run every §9 check, print a report line for each, and exit non-zero on any FAIL.

    A check that raises is caught and reported as FAIL rather than aborting the run, so
    one broken check never hides the checks after it.
    """
    failures = 0
    width = max(len(name) for _, name, _ in CHECKS)
    for item, name, check in CHECKS:
        try:
            status, detail = check()
        except Skip as exc:
            status, detail = SKIP, str(exc)
        except Exception as exc:  # a check must never mask the checks after it
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        failures += status == FAIL
        print(f"[{status:4}] §{item}  {name:<{width}}  {detail}")

    if failures:
        print(
            f"\n{failures} post-deploy step(s) still need a human — see ansible/README.md §9.",
            file=sys.stderr,
        )
        return 1
    print("\nAll §9 post-deploy steps verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
