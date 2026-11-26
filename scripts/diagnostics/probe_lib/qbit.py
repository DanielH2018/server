"""`probe.py qbit <api-path>` — read-only qBittorrent WebUI API GETs, authenticated by API key.

qBittorrent 5.2 accepts `Authorization: Bearer <key>` in place of a login, so the probe never
touches the WebUI password and never counts against `web_ui_max_auth_fail_count`. The key is
SOPS `qbittorrent_api_key`, generated in the WebUI under Options, WebUI, API Key.

The key carries full WebUI rights. The probe stays read-only because it only sends GET, and
qBittorrent 5 answers 405 to a GET on any endpoint that changes state.
"""

from diagnostics.probe_lib import core
from diagnostics.probe_lib.arr import curl_host, format_arr_response, resolve_arr_ip
from diagnostics.probe_lib.core import config_get, ha_curl_argv

from lib.kubectl import DEFAULT_CLUSTER

SERVICE = "qbittorrent"
PORT = 8080
API_KEY_NAME = "qbittorrent_api_key"


def qbit_url(ip, path):
    """Build a WebUI API URL.

    `torrents/info`, `/torrents/info`, `api/v2/torrents/info` and `v2/torrents/info` all
    resolve to `/api/v2/torrents/info`; a query string passes through untouched.
    """
    p = path.lstrip("/")
    if p.startswith("api/"):
        p = p[len("api/") :]
    if p.startswith("v2/"):
        p = p[len("v2/") :]
    return f"http://{ip}:{PORT}/api/v2/{p}"


def qbit_curl_config(api_key):
    """The `curl --config -` body carrying the bearer header (via stdin, never argv)."""
    return f'header = "Authorization: Bearer {api_key}"\n'


def run_qbit(ns):
    """Read-only qBittorrent API GET against the `qbittorrent` Service ClusterIP.

    The curl runs on the node holding the pod, for the reason `arr.curl_host` gives.
    Credential-bearing values, such as the proxy and mail passwords in `app/preferences`,
    are redacted unless `--show-secrets` is passed.
    """
    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(qbit_url("<qbittorrent-clusterip>", ns.path)))
            + "   # + Authorization: Bearer <redacted> (via --config stdin)"
        )
        return 0
    cluster = getattr(ns, "cluster", DEFAULT_CLUSTER)
    url = qbit_url(resolve_arr_ip(SERVICE, cluster), ns.path)
    body = config_get(
        url,
        qbit_curl_config(core.sops_extract(API_KEY_NAME)),
        host=curl_host(SERVICE, cluster),
    )
    text, rc = format_arr_response(
        body, as_json=ns.json, show_secrets=getattr(ns, "show_secrets", False)
    )
    print(text, end="")
    return rc
