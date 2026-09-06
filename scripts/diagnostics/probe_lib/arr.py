"""`probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
"""

import json

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import config_get, ha_curl_argv
from diagnostics.probe_lib.health_docker import resolve_service_ip

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


# --- credential redaction -------------------------------------------------------------
#
# `arr <app> notification|downloadclient|indexer|importlist` returns objects whose
# `fields[]` carry live credentials — a Discord webhook URL, the qBittorrent password, an
# indexer API key. The subcommand is read-only against the app, which says nothing about
# what it does to the transcript it prints into: one `arr sonarr notification` put the
# `arr_discord_webhook_url` value into an agent transcript on 2026-09-06 (issue #1388), and
# the exposed value then had to be rotated.
#
# Two signals decide, because neither is sufficient alone. The *arr API labels a field's
# `privacy` as `apiKey` / `password` / `userName`, but the Discord `webHookUrl` field is
# labelled `normal` and so is invisible to `privacy`. The name list is the backstop: a
# credential-bearing field is redacted as soon as it is NAMED like one, without waiting for
# upstream to relabel it.
ARR_SENSITIVE_PRIVACY = frozenset({"apiKey", "password", "userName"})

# Matched as lowercase substrings against a field's `name`, or against a plain dict key.
ARR_SENSITIVE_NAME_PARTS = (
    "apikey",
    "password",
    "passkey",
    "webhook",
    "token",
    "secret",
    "credential",
    "cookie",
    "auth",
)

REDACTED = "<redacted>"


def _name_is_sensitive(name):
    """True when a field or key name reads as credential-bearing."""
    low = str(name).lower()
    return any(part in low for part in ARR_SENSITIVE_NAME_PARTS)


def redact_arr_payload(obj):
    """Return `obj` with credential-bearing values replaced by `<redacted>`.

    Walks the whole decoded response rather than a path allow-list. The paths named in
    #1388 — notification, downloadclient, indexer, importlist — are the ones known to carry
    credentials, and a name or privacy match on any other path costs nothing.
    """
    if isinstance(obj, list):
        return [redact_arr_payload(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    # An *arr `fields[]` entry: {"name": "webHookUrl", "value": …, "privacy": "normal"}.
    # Its own `name` is the label, so the key-name walk below would never see it.
    if "name" in obj and "value" in obj:
        sensitive = obj.get("privacy") in ARR_SENSITIVE_PRIVACY or _name_is_sensitive(
            obj["name"]
        )
        if sensitive and obj["value"] not in (None, ""):
            return {**obj, "value": REDACTED}
    out = {}
    for key, value in obj.items():
        if _name_is_sensitive(key) and value not in (None, "", [], {}):
            out[key] = REDACTED
        else:
            out[key] = redact_arr_payload(value)
    return out


def format_arr_response(body, *, as_json=False, show_secrets=False):
    """Render a response body for printing, as `(text, exit_code)`.

    The redaction seam: pure, so a test asserts what reaches stdout without stubbing the
    kubectl / SOPS / curl boundary run_arr crosses to obtain `body`.
    """
    if as_json and show_secrets:
        return body, 0
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # Not JSON — an error page rather than an API object, so there is no field to walk.
        return body.strip() + "\n", 1
    if not show_secrets:
        payload = redact_arr_payload(payload)
    return json.dumps(payload, indent=None if as_json else 2) + "\n", 0


def run_arr(ns):
    """Read-only *arr API GET, resolved to the app's k8s Service ClusterIP.

    sonarr/radarr/prowlarr have run as k8s Deployments since 2026-08-07 (B4c) and have
    no Docker container left to `docker inspect` an IP from — this used to shell out to
    `resolve_ip(ns.app)`, which died with `FileNotFoundError: 'docker'` on both cluster
    nodes. resolve_arr_ip replaces it with the same idea (resolve the current address at
    run time) via kubectl instead of docker — see the comment above ARR_PORTS for why
    this talks to the Service directly instead of going through k8s_endpoint like every
    other cluster subcommand. Pulls <app>_api_key from SOPS and passes it via stdin.
    Pretty-prints JSON by default; `--json` prints it on one line. Credential-bearing
    values are redacted unless `--show-secrets` is passed — see redact_arr_payload.
    """
    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(arr_url("<arr-clusterip>", ns.app, ns.path)))
            + "   # + X-Api-Key: <redacted> (via --config stdin)"
        )
        return 0
    url = arr_url(resolve_arr_ip(ns.app), ns.app, ns.path)
    body = config_get(url, arr_curl_config(core.sops_extract(f"{ns.app}_api_key")))
    text, rc = format_arr_response(
        body, as_json=ns.json, show_secrets=getattr(ns, "show_secrets", False)
    )
    print(text, end="")
    return rc
