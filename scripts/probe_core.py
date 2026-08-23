"""Shared plumbing for probe's subcommands: endpoints, secrets, HTTP, durations.

Every domain module imports this one as ``core`` and calls through the module
(``core.sops_extract(...)``) rather than binding the names locally. That is
deliberate: several of these are monkeypatched in the tests, and a name bound
into another module's globals by ``from probe_core import sops_extract`` would
not see the patch. One module attribute, one place to patch.
"""

import os
import re
import subprocess
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

DEFAULT_TIMEOUT = 10

# Local time for every window this tool prints. The homelab is in one timezone and
# the operator reads these next to `journalctl`, so UTC would mean translating.
_CHICAGO = ZoneInfo("America/Chicago")


def ha_host():
    """HA's unsuffixed .local hostname — carries no Authelia and pointed at the same HA
    before AND after the cutover. Since the bridge teardown (slice-7 BT4) the name serves
    from the cluster edge, and host-shell DNS for it rides the Cloudflare grey-cloud
    wildcard — so callers pin it to the ingress VIP (ha_resolve) instead of trusting DNS."""
    return f"home-assistant.local.{sops_extract('domain')}"


def ha_resolve():
    """curl --resolve pin for ha_host() → the MetalLB ingress VIP (same reason as
    k8s_endpoint: the host shell's answer for the name is not the cluster edge)."""
    return f"{ha_host()}:443:{metallb_vip()}"


def ha_base():
    return f"https://{ha_host()}"


def metallb_vip():
    """The cluster's MetalLB ingress VIP, read from inventory (plaintext, not a secret)."""
    with open(GROUP_VARS_PATH) as f:
        for line in f:
            if line.startswith("k3s_metallb_ingress_vip:"):
                return line.split(":", 1)[1].strip()
    raise SystemExit(f"k3s_metallb_ingress_vip not found in {GROUP_VARS_PATH}")


def k8s_endpoint(hostname):
    """(base_url, curl --resolve pin) for a cluster route. This host's resolver bypasses
    the LAN DNS, so a `.local` name does not resolve to the cluster edge from a shell
    here; curl pins it to the MetalLB ingress VIP instead. Containers get the right
    answer from Pi-hole and need no pin."""
    host = f"{hostname}.local.{sops_extract('domain')}"
    return f"https://{host}", f"{host}:443:{metallb_vip()}"


def loki_endpoint():
    """The cluster Loki (Phase D.2 KL4)."""
    return k8s_endpoint("loki-homelab")


# claude_ha_token lives in the SOPS-encrypted secrets file (repo-root relative).
SECRETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "vars",
    "secrets.yml",
)

# Inventory group vars (plaintext) — source of the MetalLB ingress VIP.
GROUP_VARS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "inventory",
    "group_vars",
    "all.yml",
)

# Inventory hosts file (plaintext) — source of daniel-pi's LAN IP.
HOSTS_INI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "inventory",
    "hosts.ini",
)
# URL builders (pure)


def prom_query_url(base, promql):
    return f"{base}/api/v1/query?" + urlencode({"query": promql})


def prom_targets_url(base):
    return f"{base}/api/v1/targets"


def prom_endpoint():
    """The cluster prometheus via its query-only IngressRoute (the Docker prometheus —
    the old resolve_ip("prometheus") target — retired 2026-08-14 with the drain)."""
    return k8s_endpoint("prometheus")


def loki_labels_url(base):
    return f"{base}/loki/api/v1/labels"


def loki_query_url(base, logql, limit, start=None, end=None, direction=None):
    params = {"query": logql, "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    if direction is not None:
        params["direction"] = direction
    return f"{base}/loki/api/v1/query_range?" + urlencode(params)


_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration_seconds(text):
    """`30m` / `6h` / `2d` / `1w` -> seconds. Raises SystemExit on anything else.

    Loki's default query window is an hour, which silently hides anything older — a backup run
    75 minutes ago simply returns nothing, and an empty result reads as "no backups" rather than
    "you did not ask far enough back".
    """
    m = re.fullmatch(r"(\d+)([mhdw])", text.strip())
    if not m:
        raise SystemExit(f"bad duration {text!r} — use forms like 30m, 6h, 2d, 1w")
    return int(m.group(1)) * _DURATION_UNITS[m.group(2)]


def since_window_ns(since):
    """`--since` -> the (start_ns, end_ns) pair Loki's query_range takes, (None, None) if unset.

    ONE helper because two call sites need the same window and diverged when they each had
    their own copy: `plan()` builds the argv that `--dry-run`/`--json` run, while `run_query()`
    builds the URL the default formatted path fetches. `run_query()` passed no window at all,
    so the default path silently inherited Loki's server-side one-hour default while `--json`
    honoured `--since` — a 3d query returned a 60-minute slice and printed `no logs` for
    anything older. Keep both paths on this function rather than reintroducing the copy.
    """
    if not since:
        return None, None
    end_s = datetime.now(_CHICAGO).timestamp()
    start_ns = int((end_s - parse_duration_seconds(since)) * 1e9)
    return start_ns, int(end_s * 1e9)


def scrutiny_url(base):
    return f"{base}/api/summary"


PI_HOST = "daniel-pi"


def pi_url(subpath):
    return f"http://daniel-pi.lan:61208/api/4/{subpath}"


def pi_ip():
    """daniel-pi's LAN IP, read from inventory (plaintext, not a secret) — same reason as
    metallb_vip(): this host's resolver has no answer for daniel-pi.lan (a Pi-hole-only LAN
    name), so `getent hosts daniel-pi.lan` exits 2 here and curl needs a --resolve pin instead
    of DNS."""
    with open(HOSTS_INI_PATH) as f:
        for line in f:
            if line.startswith("daniel-pi ") or line.startswith("daniel-pi\t"):
                m = re.search(r"ansible_host=(\S+)", line)
                if m:
                    return m.group(1)
    raise SystemExit(f"daniel-pi ansible_host not found in {HOSTS_INI_PATH}")


def pi_resolve():
    """curl --resolve pin for pi_url()'s daniel-pi.lan:61208."""
    return f"daniel-pi.lan:61208:{pi_ip()}"


def curl_argv(url, timeout=DEFAULT_TIMEOUT, resolve=None):
    argv = ["curl", "-sS", "--max-time", str(timeout)]
    if resolve:
        argv += ["--resolve", resolve]
    argv.append(url)
    return argv


def fetch(url, resolve=None):
    """Run the read-only curl GET and return its body (raise on failure)."""
    out = subprocess.run(
        curl_argv(url, resolve=resolve), capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SystemExit(f"curl {url} failed: {out.stderr.strip()}")
    return out.stdout


def k8s_namespace():
    """The workload namespace, read from inventory (plaintext, not a secret)."""
    with open(GROUP_VARS_PATH) as f:
        for line in f:
            if line.startswith("k8s_namespace:"):
                return line.split(":", 1)[1].strip()
    raise SystemExit(f"k8s_namespace not found in {GROUP_VARS_PATH}")


def sops_extract(key_name):
    """Decrypt a single top-level key from the SOPS secrets file. Requires the
    host's age key (present on daniel-server)."""
    out = subprocess.run(
        ["sops", "-d", "--extract", f'["{key_name}"]', SECRETS_PATH],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            f"could not decrypt {key_name} from {SECRETS_PATH}: {out.stderr.strip()}"
        )
    return out.stdout.strip()


def _rows_from_loki(data: dict) -> list[tuple[int, str]]:
    """Flatten a Loki query_range response into a time-sorted list of (ns_ts, line)
    tuples across all returned streams."""
    rows = [
        (int(ts), line)
        for stream in (data.get("data") or {}).get("result") or []
        for ts, line in stream.get("values") or []
    ]
    rows.sort()
    return rows


def ha_curl_argv(url, timeout=DEFAULT_TIMEOUT, resolve=None):
    """curl argv for an HA GET. The bearer header is fed via stdin (`--config -`,
    see ha_curl_config), so the token NEVER appears in argv / `ps` / shell history."""
    argv = ["curl", "-sS", "--max-time", str(timeout), "--config", "-"]
    if resolve:
        argv += ["--resolve", resolve]
    return argv + [url]


def config_get(url, config_body, resolve=None):
    """Authenticated GET whose auth header is fed via curl `--config -` stdin
    (never argv). Returns the response body."""
    out = subprocess.run(
        ha_curl_argv(url, resolve=resolve),
        input=config_body,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"curl {url} failed: {out.stderr.strip()}")
    return out.stdout
