"""`probe.py arr <app> <api-path>` — read-only *arr API GETs against sonarr/radarr/prowlarr/lidarr.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.
"""

import json
import socket


# Use `core.<name>` for anything tests monkeypatch: a `from core import` binds a snapshot the patch
# never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import config_get, ha_curl_argv
from diagnostics.probe_lib.health_docker import resolve_service_ip
from diagnostics.probe_lib.vip_placement import ready_endpoint_nodes

from lib.kubectl import DEFAULT_CLUSTER, kubectl_json

# Not via k8s_endpoint: Sonarr has no Authelia bypass for /api/*, so a Traefik GET 302s to login.
# Mirrors ARR_PORTS / ARR_API_VERSIONS in arr-notification's seed_arr_notification.py.
ARR_PORTS = {"sonarr": 8989, "radarr": 7878, "prowlarr": 9696, "lidarr": 8686}
ARR_API_VERSION = {"sonarr": "v3", "radarr": "v3", "prowlarr": "v1", "lidarr": "v1"}


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


def resolve_arr_ip(app, cluster=DEFAULT_CLUSTER):
    """Resolve the *arr app's k8s Service ClusterIP.

    Used instead of k8s_endpoint because sonarr/radarr/prowlarr
    have no Authelia bypass rule for /api/* (see the comment above ARR_PORTS). A ClusterIP is
    stable across pod restarts and redeploys, so this doesn't reintroduce the hand-copied-IP
    staleness `docker inspect` was resolving around in the first place. The ClusterIP answers
    a host process only on the pod's own node: see `curl_host`.
    """
    return resolve_service_ip(app, cluster)


def curl_host(app, cluster=DEFAULT_CLUSTER):
    """The node to run the curl on, or None to run it on this host.

    A host process reaches a pod on its own node from that node's cni0 gateway, which
    netpol-baseline admits. A request to a pod on the other node crosses the overlay and
    arrives from the sender's flannel.1 address, which no *arr fence admits
    (`k3s_flannel_node_ips` in k8s/vars.yml says why). So the curl runs on the node that
    holds the app's ready endpoint. An app with no ready endpoint runs locally, and curl
    reports the failure.

    Raises:
        core.Unreadable: kubectl could not read the app's EndpointSlices.
    """
    doc = kubectl_json(
        cluster,
        "-n",
        core.k8s_namespace(),
        "get",
        "endpointslices",
        "-l",
        f"kubernetes.io/service-name={app}",
        "-o",
        "json",
    )
    if doc is None:
        raise core.Unreadable(f"kubectl could not read {app}'s EndpointSlices")
    nodes = ready_endpoint_nodes(doc.get("items") or [], app)
    here = socket.gethostname().split(".")[0]
    if not nodes or here in nodes:
        return None
    return min(nodes)


# `fields[]` carry live credentials; redact on `privacy` and on the field name, since Discord's
# `webHookUrl` is labelled `normal`.
ARR_SENSITIVE_PRIVACY = frozenset({"apiKey", "password", "userName"})

# Matched as lowercase substrings against a field's `name`, or against a plain dict key.
ARR_SENSITIVE_NAME_PARTS = core.SECRET_NAME_PARTS

REDACTED = core.REDACTED


def _name_is_sensitive(name):
    """True when a field or key name reads as credential-bearing."""
    low = str(name).lower()
    return any(part in low for part in ARR_SENSITIVE_NAME_PARTS)


# A credential can also sit inside a value. A Prowlarr proxy download URL in a history record's
# `data.downloadUrl` carries Prowlarr's API key under a dict key no name rule matches,
# and a log message can quote the same URL, so the walk below passes every string through
# `core.redact_query_params` rather than only the URL-shaped keys.


def redact_arr_payload(obj):
    """Return `obj` with credential-bearing values replaced by `<redacted>`.

    Walks the whole decoded response rather than a path allow-list. The paths notification,
    downloadclient, indexer and importlist are the ones known to carry credentials, and a
    name or privacy match on any other path costs nothing. Every string value is also
    scanned for credential-named URL query parameters (`core.redact_query_params`).
    """
    if isinstance(obj, list):
        return [redact_arr_payload(item) for item in obj]
    if isinstance(obj, str):
        return core.redact_query_params(obj)
    if not isinstance(obj, dict):
        return obj
    # A `fields[]` entry's own `name` is the label, so the key-name walk below would never see it.
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

    sonarr/radarr/prowlarr/lidarr run as k8s Deployments and have no Docker container to
    `docker inspect` an IP from, so resolve_arr_ip resolves the current address at run
    time via kubectl — see the comment above ARR_PORTS for why this talks to the Service
    directly instead of going through k8s_endpoint like every other cluster subcommand.
    Pulls <app>_api_key from SOPS and passes it via stdin. Pretty-prints JSON by
    default; `--json` prints it on one line. Credential-bearing values are redacted
    unless `--show-secrets` is passed — see redact_arr_payload.
    """
    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(arr_url("<arr-clusterip>", ns.app, ns.path)))
            + "   # + X-Api-Key: <redacted> (via --config stdin)"
        )
        return 0
    cluster = getattr(ns, "cluster", DEFAULT_CLUSTER)
    url = arr_url(resolve_arr_ip(ns.app, cluster), ns.app, ns.path)
    body = config_get(
        url,
        arr_curl_config(core.sops_extract(f"{ns.app}_api_key")),
        host=curl_host(ns.app, cluster),
    )
    text, rc = format_arr_response(
        body, as_json=ns.json, show_secrets=getattr(ns, "show_secrets", False)
    )
    print(text, end="")
    return rc
