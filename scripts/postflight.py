#!/usr/bin/env python3
"""Verify the post-deploy setup that Ansible can't do (ansible/README.md §9).

Every step in §9 lives in an app's own database, and most of them fail SILENTLY —
the container stays healthy while the feature behind it does nothing. This script
exercises each one against the live host and exits non-zero naming the README item
that still needs a human.

    uv run python scripts/postflight.py

Read-only. Credentials come from SOPS and are handed to curl via `--config -` on
stdin, never argv, so they never reach `ps` or shell history (same guard as probe.py).
A check whose container isn't deployed here reports SKIP, not a failure — the Pi runs
almost none of these.
"""

import json
import subprocess
import sys

import probe

TIMEOUT = 10

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


class Skip(Exception):
    """This host doesn't run the service, so the README item doesn't apply to it."""


def get(url, header=None, timeout=TIMEOUT, resolve=None):
    """GET url, returning (http_status, body). `header` is a full `curl --config`
    body (e.g. `header = "X-Api-Key: ..."`) fed via stdin so credentials stay out
    of argv. `resolve` is a curl --resolve pin (probe.k8s_endpoint's second element)
    for cluster routes the host shell can't resolve. status 0 means curl itself failed
    (connection refused, DNS, timeout)."""
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


def container_ip(name):
    """The container's bridge IP, or Skip if it isn't running on this host."""
    try:
        return probe.resolve_ip(name)
    except SystemExit as exc:
        raise Skip(str(exc)) from exc


def secret(name):
    try:
        value = probe.sops_extract(name)
    except SystemExit as exc:
        return "", str(exc)
    return value, ""


# §9.1 + §9.2: Uptime-Kuma
# Both are checked through Prometheus rather than Kuma itself: Kuma 2.x drives its
# admin wizard and API-key minting over Socket.IO only, so there is no REST route to
# ask "does an admin exist". The scrape is the observable consequence of both steps.


def _cluster_prom_query(promql):
    """Query the CLUSTER prometheus through its LAN query route (the uptime-kuma job
    moved there at the Phase D dashboard triage, PG1). VIP-pinned — this host's
    resolver bypasses the LAN DNS, so the name alone does not reach the cluster edge."""
    base, pin = probe.k8s_endpoint("prometheus")
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
    with open(probe.STATIC_MONITORS_PATH) as f:
        declared = probe.parse_declared_monitors(f.read())
    text, code = probe.format_kuma_drift(declared, live, probe.kuma_pod_age_seconds())
    return (FAIL if code else OK), text.replace("\n", "; ").strip()


def check_kuma_scrape():
    """§9.2 — a stale prometheus_kuma_api_key leaves the uptime-kuma target at 401.
    Reads `up{job=...}` rather than the targets API: the cluster route only admits
    /api/v1/query paths, and up==0 is the same evidence the target listing gave."""
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


def check_arr_key(app):
    ip = container_ip(app)
    key, err = secret(f"{app}_api_key")
    if not key:
        return FAIL, err
    status, _ = get(probe.arr_url(ip, app, "system/status"), probe.arr_curl_config(key))
    if status == 200:
        return OK, f"{app}_api_key authenticates"
    return FAIL, f"HTTP {status} — {app}_api_key doesn't match the app's own key"


def check_jellyfin_key():
    ip = container_ip("jellyfin")
    key, err = secret("jellyfin_api_key")
    if not key:
        return FAIL, err
    status, _ = get(
        f"http://{ip}:8096/System/Info", f'header = "X-Emby-Token: {key}"\n'
    )
    if status == 200:
        return OK, "jellyfin_api_key authenticates"
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
    # Since slice-5 B3 HA runs in the cluster — probe.ha_base() is the bridge URL, the same
    # endpoint before and after the cutover (no container to inspect).
    token, err = secret(name)
    if not token:
        return FAIL, err
    status, _ = get(probe.ha_get_url(probe.ha_base(), ""), probe.ha_curl_config(token))
    if status == 200:
        return OK, "token accepted"
    return FAIL, f"HTTP {status} — re-mint under Profile → Security"


# §9.5: Authelia
# The role asserts the OIDC material exists, so a missing secret fails the deploy
# rather than failing silently. What it can't tell you is whether the running
# instance actually came up with it.


def check_authelia():
    ip = container_ip("authelia")
    status, body = get(f"http://{ip}:9091/api/health")
    if status != 200:
        return FAIL, f"HTTP {status} — Authelia is not serving"
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
