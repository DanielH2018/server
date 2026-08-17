#!/usr/bin/env python3
"""Read-only homelab diagnostics — one allow-listed surface for the queries that
used to be hand-written `curl`/`openssl` one-offs.

The monitoring stack (Prometheus, Loki, Scrutiny, uptime-kuma) does NOT publish
host ports; it's internal to the Docker network. The old approach was to
`curl http://<bridge-ip>:<port>/...` against a hand-copied container IP — but
Docker reassigns those IPs on recreate, so every such allow-list entry was dead
on the next deploy. This wrapper resolves the container's *current* IP via
`docker inspect` at run time, so it keeps working, and a single allow-list entry
covers every subcommand:

    Bash(uv run python scripts/probe.py:*)

Everything it runs is read-only (HTTP GET / TLS handshake / docker inspect).

Subcommands:
    metric '<promql>'        Prometheus instant query [--json] (prometheus :9090)
    targets                  Prometheus scrape-target health (prometheus :9090)
    monitors                 Kuma down-monitors rollup (exit 0 = all up), read from the
                             monitor_status metric Prometheus already scrapes — no Kuma
                             API credential needed
    loki-labels              Loki label names                (cluster loki-homelab)
    loki-query '<logql>'     Loki range query [--limit N] [--json] (cluster loki-homelab)
    alerts                   monitor-bridge DOWN history as episodes [--days N --check X --raw --json]
    scrutiny                 Disk SMART summary              (cluster scrutiny, both nodes)
    pi <subpath>             Pi glances API, e.g. `pi fs`    (daniel-pi.lan:61208)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]
    health <service>         k8s rollout + recent-restart rollup (exit 0 = healthy)
                             [--docker inspects the Pi's container instead]
    arr <app> <api-path>     Read-only *arr API GET [--json] (sonarr/radarr/prowlarr)
    ha state <entity_id>     Live HA entity state + attrs    (home-assistant :8123)
    ha automation <id|alias> One automation's on/off + last_triggered (resolves alias!=id)
    ha get <api-path>        Raw GET /api/<path>, e.g. `ha get error_log`
    ha trace <id|alias>      Why an automation last ran/no-op'd (per-condition WS trace; alias: why)
    ha verify-automations    Assert every automation in automations.yaml loaded (exit 0 = all loaded)
    ha verify-entities       Assert every entity in external_entities.yml still exists live
    ha-state [--inventory]   Live view of the derived HA state model

`metric` and `loki-query` print a formatted view by default (one `<labels> = <value>`
line per series; log lines oldest→newest) so you don't need to pipe into `python3 -c`
to reshape the JSON — pass `--json` for the raw response.

`ha` is read-only (GET) and authenticates with the SOPS-encrypted claude_ha_token
(server-only — needs the host age key). The token is fed to curl via stdin, never argv.
`arr` works the same way — it pulls `<app>_api_key` from SOPS and passes it via stdin,
so the *arr key never lands in argv / `ps` / shell history.
Add `--dry-run` to print the command(s) instead of running them.

NB: `cert <public-host>` shows the CLOUDFLARE EDGE cert, NOT Traefik's origin cert — public DNS
resolves the host to Cloudflare, so the TLS handshake terminates there. To inspect the origin
Let's Encrypt cert, point at the origin IP with the host as SNI:
`cert <server-ip>:443 --sni <host>` (origin expiry is also independently watched by
monitor-bridge's TLS Cert Expiry monitor).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

DEFAULT_TIMEOUT = 10


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

# Git-managed automation source (repo-root relative to this file) — the "expected" set for
# the verify-automations post-deploy gate. The deployed config is copied from here verbatim.
# `k8s`, not `containers`: HA moved at the slice-5 B3 cutover and this constant did not follow,
# so the gate raised FileNotFoundError from the cutover until the 2026-08-16 review. The old
# test only asserted argparse wiring and never opened the file — test_verify_automations_path_exists
# now pins the path itself.
AUTOMATIONS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "roles",
    "k8s",
    "home-assistant",
    "files",
    "automations.yaml",
)

# Top-level automation list items only: `- id: <slug>` anchored at column 0. A trigger/condition
# `id:` is always indented, so it can never be mistaken for an automation id.
_AUTOMATION_ID_RE = re.compile(r"^- id:\s*(\S+)", re.MULTILINE)

# The generated snapshot of integration-provided entities that validate_ha_config.py resolves
# config references against — the "expected" set for the verify-entities gate.
EXTERNAL_ENTITIES_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "roles",
    "k8s",
    "home-assistant",
    "state",
    "external_entities.yml",
)

# `  - domain.object_id` list items. Matched by regex rather than a YAML parse to keep probe.py
# dependency-free, consistent with expected_automation_ids above.
_SNAPSHOT_ENTITY_RE = re.compile(r"^\s*-\s+([a-z_]+\.[A-Za-z0-9_]+)\s*$", re.MULTILINE)

# --- URL builders (pure) ----------------------------------------------------


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


# B2 charges per transaction class and reports the totals nowhere an API can reach: the Native
# API has no usage operation, and the per-class Usage Reports are Partner-tier. Backup spend is
# recoverable from Longhorn's logs (see BACKUP_BLOCKS_RE), but MAINTENANCE spend — the drains,
# inventories and verification listings an operator runs by hand — leaves no trace at all once
# the terminal scrolls. On 2026-08-17 that was most of a 2,000-transaction day and had to be
# reconstructed from memory, badly. Anything here that talks to B2 records what it spent, so the
# controllable half of the bill stops being guesswork.
B2_LEDGER_DIR = os.path.expanduser("~/.local/state/homelab/b2-ledger")


def b2_ledger_path(day=None):
    """One TSV per UTC day — the day B2's own counters reset on, not the local day."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(B2_LEDGER_DIR, f"{day}.tsv")


def record_b2_spend(tool, class_a=0, class_b=0, class_c=0, note="", _now=None):
    """Append one line of spend. Never raises: a ledger failure must not fail the real work."""
    try:
        os.makedirs(B2_LEDGER_DIR, exist_ok=True)
        stamp = (_now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = "\t".join(
            [
                stamp,
                tool,
                str(class_a),
                str(class_b),
                str(class_c),
                note.replace("\t", " "),
            ]
        )
        with open(b2_ledger_path(), "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def parse_b2_ledger(lines):
    """TSV lines -> per-tool {class_a, class_b, class_c, runs}. Malformed lines are skipped."""
    tools = {}
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        try:
            a, b, c = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        entry = tools.setdefault(
            parts[1], {"runs": 0, "class_a": 0, "class_b": 0, "class_c": 0}
        )
        entry["runs"] += 1
        entry["class_a"] += a
        entry["class_b"] += b
        entry["class_c"] += c
    return tools


def read_b2_ledger(day=None):
    try:
        with open(b2_ledger_path(day)) as fh:
            return parse_b2_ledger(fh.readlines())
    except OSError:
        return {}


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


# Longhorn logs one of these per backup, naming the delta it processed:
#   "Created snapshot changed blocks: 46 mappings, 46 blocks and 45 new blocks"
# `blocks` is the delta it walks, and it issues one HeadObject per block to decide whether the
# block is already in the store — so that count IS the backup's Class B transaction cost. `new`
# is what it then uploaded, which is Class A and unmetered. B2 publishes no usage endpoint (its
# per-class counters are Partner-tier only), so this line is the closest thing to a meter the
# backup plane has, and it costs nothing because the logs are already in Loki.
BACKUP_BLOCKS_RE = re.compile(
    r"Created snapshot changed blocks: \d+ mappings, (\d+) blocks and (\d+) new blocks"
)
# The replica that emitted the line, e.g. "[pvc-1c0e18da-...-r-9d333575] time=..."
BACKUP_VOLUME_RE = re.compile(r"\[(pvc-[0-9a-f-]{36})-r-[0-9a-f]+\]")
SPEND_LOGQL = '{namespace="longhorn-system"} |= "Created snapshot changed blocks"'


def parse_backup_spend(rows):
    """[(ns_timestamp, line)] -> per-volume {backups, blocks, new_blocks}.

    Lines whose replica prefix was trimmed by the log pipeline still count toward the totals;
    they are attributed to "unattributed" rather than dropped, because losing them would
    understate spend and understating is the failure mode that matters here.
    """
    vols = {}
    for _, line in rows:
        m = BACKUP_BLOCKS_RE.search(line)
        if not m:
            continue
        v = BACKUP_VOLUME_RE.search(line)
        name = v.group(1) if v else "unattributed"
        entry = vols.setdefault(name, {"backups": 0, "blocks": 0, "new_blocks": 0})
        entry["backups"] += 1
        entry["blocks"] += int(m.group(1))
        entry["new_blocks"] += int(m.group(2))
    return vols


def format_backup_spend(vols, window, names=None, ledger=None):
    """Render measured backup spend alongside recorded maintenance spend.

    Never exits non-zero: this is a meter, not a gate. The two halves are printed separately and
    deliberately NOT summed — the backup figure spans --since while the ledger covers the UTC day
    B2's counters reset on, so a combined total would match neither window.
    """
    names = names or {}
    rows = []
    if vols:
        rows.append("%-24s %8s %8s %10s" % ("PVC", "BACKUPS", "BLOCKS", "UPLOADED"))
        for vol in sorted(vols, key=lambda k: -vols[k]["blocks"]):
            v = vols[vol]
            rows.append(
                "%-24s %8d %8d %10d"
                % (names.get(vol, vol)[:24], v["backups"], v["blocks"], v["new_blocks"])
            )
        rows.append("")
        rows.append(
            "backups over %s: %d Class B measured, %d blocks uploaded (Class A, unmetered)"
            % (
                window,
                sum(v["blocks"] for v in vols.values()),
                sum(v["new_blocks"] for v in vols.values()),
            )
        )
    else:
        rows.append(
            f"no backups logged in the last {window} — widen --since, or nothing ran"
        )

    ledger = ledger or {}
    rows.append("")
    if ledger:
        rows.append("maintenance recorded today (UTC), from the ledger:")
        for tool in sorted(ledger, key=lambda k: -ledger[k]["class_c"]):
            t = ledger[tool]
            rows.append(
                "  %-22s %3d run(s) %6d Class B %6d Class C"
                % (tool[:22], t["runs"], t["class_b"], t["class_c"])
            )
        rows.append(
            "  %-22s %10d Class B %6d Class C"
            % (
                "TOTAL",
                sum(t["class_b"] for t in ledger.values()),
                sum(t["class_c"] for t in ledger.values()),
            )
        )
    else:
        rows.append(
            "no maintenance recorded today — nothing has written the ledger yet"
        )

    rows.append("")
    rows.append(
        "Still unrecorded: Longhorn's own metadata reads (Backup-CR pulls, target syncs) and "
        "the monitor's B2 probes. B2 publishes no counter to reconcile against, so the console's "
        "Caps & Alerts page remains the only ground truth."
    )
    return "\n".join(rows)


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


# --- Home Assistant (pure) --------------------------------------------------


def ha_state_url(base, entity_id):
    return f"{base}/api/states/{entity_id}"


def ha_get_url(base, path):
    """URL for an arbitrary HA REST path under `base` (scheme://host, no trailing slash).
    Normalizes a leading `/` and an `api/` prefix so `error_log`, `/error_log`, and
    `/api/error_log` all work."""
    path = path.lstrip("/")
    if path.startswith("api/"):
        path = path[len("api/") :]
    return f"{base}/api/{path}"


def ha_curl_argv(url, timeout=DEFAULT_TIMEOUT, resolve=None):
    """curl argv for an HA GET. The bearer header is fed via stdin (`--config -`,
    see ha_curl_config), so the token NEVER appears in argv / `ps` / shell history."""
    argv = ["curl", "-sS", "--max-time", str(timeout), "--config", "-"]
    if resolve:
        argv += ["--resolve", resolve]
    return argv + [url]


def ha_curl_config(token):
    """The `curl --config -` body carrying the auth header (consumed via stdin)."""
    return f'header = "Authorization: Bearer {token}"\n'


# --- *arr apps (sonarr/radarr/prowlarr) read-only API (pure) ----------------
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
    """Build an *arr API URL. Normalizes a leading `/`, an `api/` prefix, and a
    redundant version segment so `health`, `/health`, `api/v3/health`, and
    `v3/health` all resolve to the app's correct `/api/<ver>/health`."""
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


# --- Minimal synchronous WebSocket client (stdlib only — no `websockets` dep) -----------------
# Used ONLY for the read-only automation-trace API (Task: ha trace/why). A client text frame MUST
# be masked (RFC 6455); server frames are unmasked. We assume one JSON message per unfragmented
# frame, which is how HA sends WS responses.


def _ws_encode(payload: str) -> bytes:
    """A single masked client text frame (FIN=1, opcode=0x1)."""
    import os
    import struct

    data = payload.encode()
    n = len(data)
    header = bytearray([0x81])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    header += mask
    return bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _ws_read_frame(recv_exact) -> str:
    """Decode one unmasked server text frame, reading exact byte counts via recv_exact(n)->bytes."""
    import struct

    recv_exact(1)  # b0: FIN+opcode (text, unfragmented — not inspected)
    length = recv_exact(1)[0] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(8))[0]
    return recv_exact(length).decode()


def _recv_exact_from(sock):
    """Return a recv_exact(n)->bytes reader over a socket, buffering across recv() boundaries."""
    buf = bytearray()

    def recv_exact(n: int) -> bytes:
        while len(buf) < n:
            chunk = sock.recv(4096)
            if not chunk:
                raise SystemExit("HA websocket closed unexpectedly")
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    return recv_exact


def format_trace(trace) -> str:
    """Human timeline from a trace/get result: trigger -> each step path (+ PASS/FAIL for a
    condition step, whose result is {"result": bool}) -> error.

    HA's trace/get payload has `trigger` as a plain string description (e.g.
    "state of binary_sensor.aqara_fp300_presence"); older/nested shapes may be a dict
    with a `description` key — both are handled."""
    if not trace:
        return (
            "no stored trace (the automation hasn't run since the last HA restart/deploy; "
            "an automation whose trigger never matched leaves no trace — check `ha get "
            "logbook/<entity>` and the automation's last_triggered for that case)"
        )
    lines = []
    trig = trace.get("trigger") or {}
    if isinstance(trig, dict):
        trig_desc = trig.get("description", trig)
    else:
        trig_desc = trig
    lines.append(f"trigger: {trig_desc}")
    for path, steps in (trace.get("trace") or {}).items():
        for step in steps:
            res = step.get("result")
            verdict = ""
            if isinstance(res, dict) and isinstance(res.get("result"), bool):
                verdict = "  -> PASS" if res["result"] else "  -> FAIL (blocked here)"
            lines.append(f"  {path}{verdict}")
    if trace.get("error"):
        lines.append(f"error: {trace['error']}")
    return "\n".join(lines)


def expected_automation_ids(text: str) -> set[str]:
    """The `id:` of every top-level automation in automations.yaml text. Regex over the raw
    text (no YAML parse) — robust to the HA Jinja inside the file; ids are simple slugs."""
    return set(_AUTOMATION_ID_RE.findall(text))


def snapshot_entity_ids(text: str) -> set[str]:
    """Every `domain.object_id` listed in external_entities.yml text."""
    return set(_SNAPSHOT_ENTITY_RE.findall(text))


def vanished_snapshot_entities(snapshot_ids, live_entity_ids):
    """Ids present in state/external_entities.yml that no longer exist live, sorted.

    The snapshot is what validate_ha_config.py resolves config references against, and it is
    only rewritten by an explicit `ha_state_model.py refresh`. That makes the resolution guard
    good at catching a TYPO and structurally blind to a DISAPPEARANCE: an integration entity
    that goes away stays in the snapshot, so every reference to it keeps validating clean while
    `states()` quietly returns 'unknown' at runtime. That is exactly how
    sensor.pixel_9_pro_do_not_disturb_sensor and _sleep_duration disabled three bedroom features
    without any check going red (2026-08-16 review). This turns that class into a live gate.
    """
    live = set(live_entity_ids)
    return sorted(e for e in set(snapshot_ids) if e not in live)


def automation_load_errors(expected_ids, live_automations):
    """expected_ids = ids from automations.yaml; live_automations = the automation.* entries
    from /api/states. A defined id with no live automation carrying that attributes.id did NOT
    load (dropped). A defined id whose live automation is `unavailable` errored at load. A
    disabled automation (state 'off') is fine. Live ids not in the file (UI/.storage cruft) are
    ignored — this gate is file-driven so cruft can't make it red."""
    by_id = {}
    for a in live_automations:
        aid = (a.get("attributes") or {}).get("id")
        if aid is not None:
            by_id[aid] = a
    errs = []
    for aid in sorted(expected_ids):
        live = by_id.get(aid)
        if live is None:
            errs.append(
                f"automation {aid} is defined in automations.yaml but did not load"
            )
        elif live.get("state") == "unavailable":
            errs.append(
                f"automation {aid} loaded but is unavailable (config error at load)"
            )
    return errs


def _ws_send(sock, msg):
    import json

    sock.sendall(_ws_encode(json.dumps(msg)))


def _ws_recv_json(recv_exact):
    import json

    return json.loads(_ws_read_frame(recv_exact))


def ha_trace(host, token, automation_id, timeout=DEFAULT_TIMEOUT, connect_ip=None):
    """Fetch the latest execution trace for an automation via the HA WebSocket API. Read-only:
    sends ONLY auth + trace/list + trace/get. Returns the trace dict, or None if no stored trace.

    `host` is the unsuffixed .local hostname (TLS on 443, SNI/Host). `connect_ip` pins the
    TCP connection to the ingress VIP — since the bridge teardown (slice-7 BT4) the host
    shell's DNS answer for the name is not the cluster edge (see ha_host)."""
    import base64
    import os
    import socket
    import ssl

    raw = socket.create_connection((connect_ip or host, 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET /api/websocket HTTP/1.1\r\nHost: {host}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        recv_exact = _recv_exact_from(sock)
        # consume the HTTP 101 upgrade response (headers end with a blank line)
        header = b""
        while b"\r\n\r\n" not in header:
            header += recv_exact(1)
        _ws_recv_json(recv_exact)  # auth_required
        _ws_send(sock, {"type": "auth", "access_token": token})
        if _ws_recv_json(recv_exact).get("type") != "auth_ok":
            raise SystemExit("HA websocket auth failed (check claude_ha_token)")
        _ws_send(
            sock,
            {
                "id": 1,
                "type": "trace/list",
                "domain": "automation",
                "item_id": automation_id,
            },
        )
        listed = _ws_recv_json(recv_exact).get("result") or []
        if not listed:
            return None
        run_id = listed[-1]["run_id"]
        _ws_send(
            sock,
            {
                "id": 2,
                "type": "trace/get",
                "domain": "automation",
                "item_id": automation_id,
                "run_id": run_id,
            },
        )
        return _ws_recv_json(recv_exact).get("result")
    finally:
        sock.close()


def _slug(name):
    """HA-style slug: lowercase, non-alphanumerics collapsed to single `_`."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def match_automation(states, query):
    """Find an automation in a `/api/states` list by entity_id, `attributes.id`,
    or friendly-name slug. Resolves the alias-slug-vs-id trap: an automation's
    entity_id derives from its *alias*, not its `id`, so the two can differ
    (e.g. id `bedroom_fan_temperature` -> `automation.bedroom_fan_temperature_control`).
    Accepts a bare slug/id or a full `automation.<slug>` entity_id. None if no match."""
    want_entity = query if query.startswith("automation.") else "automation." + query
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("automation."):
            continue
        attrs = s.get("attributes") or {}
        if eid == want_entity:
            return s
        if attrs.get("id") == query:
            return s
        if _slug(attrs.get("friendly_name")) == query:
            return s
    return None


def format_ha_state(obj):
    """One-to-two-line human summary of a single `/api/states/<entity>` object."""
    attrs = obj.get("attributes") or {}
    head = f"{obj.get('entity_id', '?')} = {obj.get('state')}"
    name = attrs.get("friendly_name")
    if name:
        head += f"  ({name})"
    lc, lu = obj.get("last_changed"), obj.get("last_updated")
    tail = []
    if lc:
        tail.append(f"last_changed={lc}")
    if lu and lu != lc:
        tail.append(f"last_updated={lu}")
    return head + ("\n  " + "  ".join(tail) if tail else "")


def format_ha_automation(obj):
    """Human summary of an automation state — on/off + id + last_triggered."""
    attrs = obj.get("attributes") or {}
    return (
        f"{obj.get('entity_id', '?')} = {obj.get('state')}  "
        f"({attrs.get('friendly_name', '?')})\n"
        f"  id={attrs.get('id')}  last_triggered={attrs.get('last_triggered')}"
    )


def ha_state_rows(states, model):
    """Render the derived cells/automations annotated with live values from a /api/states list."""
    by_id = {s["entity_id"]: s for s in states}
    lines = ["Cells:"]
    for name, cell in model["cells"].items():
        s = by_id.get(cell["entity"])
        val = s["state"] if s else "—(absent)"
        when = s.get("last_changed", "") if s else ""
        lines.append(f"  {cell['entity']:<52} = {val:<12} {when}")
    anomalies = []
    sleep = by_id.get("input_boolean.bedroom_sleep_mode", {}).get("state")
    if sleep == "on":
        anomalies.append("sleep_mode is on (verify expected at this hour)")
    moff = by_id.get("input_boolean.bedroom_manual_off", {}).get("state")
    if moff == "on":
        anomalies.append("manual_off is on (presence will NOT auto-light)")
    if anomalies:
        lines = [
            f"⚠ {len(anomalies)} anomaly(ies): " + "; ".join(anomalies),
            "",
        ] + lines
    return "\n".join(lines)


# --- low-level argv / parsing helpers (pure) --------------------------------


def curl_argv(url, timeout=DEFAULT_TIMEOUT, resolve=None):
    argv = ["curl", "-sS", "--max-time", str(timeout)]
    if resolve:
        argv += ["--resolve", resolve]
    argv.append(url)
    return argv


def inspect_ip_argv(container):
    return [
        "docker",
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        container,
    ]


def parse_ip(inspect_output):
    """First non-empty token of `docker inspect`'s IP list (host can reach any
    of a container's bridge IPs). None if the container has no address."""
    for tok in inspect_output.split():
        if tok:
            return tok
    return None


def inspect_argv(container):
    return ["docker", "inspect", container]


def k8s_service_ip_argv(service, namespace):
    """kubectl argv for a Service's ClusterIP — the k8s analog of inspect_ip_argv, for
    apps (arr) that must be reached directly rather than through k8s_endpoint."""
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "service",
        service,
        "-o",
        "jsonpath={.spec.clusterIP}",
    ]


def format_health(data, container):
    """Summarize a container's state + healthcheck from `docker inspect` output.

    Pure: takes the parsed JSON list and returns (text, exit_code). exit_code is 0
    only when the container is running and (has no healthcheck, or is healthy) — so
    `probe.py health <svc>` is usable as a post-deploy gate.
    """
    if not data:
        return (
            f"{container}: not found (not created — wrong name, or deploy failed?)",
            1,
        )
    state = data[0].get("State") or {}
    status = state.get("Status", "unknown")
    restarts = data[0].get("RestartCount", 0)
    health = state.get("Health")
    if health:
        hstatus = health.get("Status", "unknown")
        line = f"{container}: {status}, health={hstatus}, restarts={restarts}"
        if hstatus != "healthy":
            line += f" — failing streak {health.get('FailingStreak', 0)}"
            log = health.get("Log") or []
            last = (log[-1].get("Output") or "").strip().splitlines() if log else []
            if last:
                line += f"; last check: {last[-1][:160]}"
        return (line, 0 if status == "running" and hstatus == "healthy" else 1)
    return (
        f"{container}: {status} (no healthcheck), restarts={restarts}",
        0 if status == "running" else 1,
    )


# A container that crashlooped this recently is not healthy, however ready it reads right now.
# Ansible's post-rollout gate soaks for 60s (k8s_rollout_stabilise_seconds) watching the restart
# COUNT, because a pod that crashes and recovers within a second passes every readiness-derived
# field — see roles/k8s/manifests/tasks/assert_stable.yml. probe.py takes ONE sample instead of
# soaking, so it reads the same signal from the other end: how long ago the last restart was.
# Wider than the Ansible window, since a single sample can land anywhere in a crash cycle.
RECENT_RESTART_SECONDS = 180


def k8s_deploy_argv(service, namespace, kind="deploy"):
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        kind,
        service,
        "-o",
        "json",
    ]


def k8s_pods_argv(service, namespace):
    return [
        "k3s",
        "kubectl",
        "-n",
        namespace,
        "get",
        "pods",
        "-l",
        f"app={service}",
        "-o",
        "json",
    ]


def _seconds_since(timestamp, now):
    """Seconds between an RFC3339 kubectl timestamp and `now`, or None if unparseable."""
    if not timestamp:
        return None
    try:
        when = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError, TypeError:
        return None
    return (now - when).total_seconds()


def _rollout_counts(workload):
    """(desired, updated, ready, available) for a Deployment or a DaemonSet.

    DaemonSets carry the same four numbers under different names, and `desired` is the count of
    nodes the scheduler picked rather than a spec field — so a DaemonSet pinned to one node is
    complete at 1/1, not at one-per-node.
    """
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    if workload.get("kind") == "DaemonSet":
        return (
            status.get("desiredNumberScheduled", 0),
            status.get("updatedNumberScheduled", 0),
            status.get("numberReady", 0),
            status.get("numberAvailable", 0),
        )
    return (
        spec.get("replicas", 1),
        status.get("updatedReplicas", 0),
        status.get("readyReplicas", 0),
        status.get("availableReplicas", 0),
    )


def format_k8s_health(deploy, pods, service, now):
    """Summarize a workload's rollout + its pods' restarts. Returns (text, exit_code).

    Pure: takes the two parsed kubectl JSON documents and a `now`, returns what to print.
    `deploy` is a Deployment or a DaemonSet — six workloads here are DaemonSets (promtail,
    node-exporter, the crowdsec node agent, dri-device-plugin, ...) and a gate that silently
    could not check them would be a gate with holes exactly where the node-level agents are.

    exit_code is 0 only when the rollout is COMPLETE (the observed generation has caught up and
    every replica is updated, ready and available) AND no container restarted within
    RECENT_RESTART_SECONDS. Both halves are load-bearing: readiness alone flips Available before
    a bad liveness probe starts killing the container, which is the failure that produced a green
    deploy and a crashlooping kube-state-metrics on 2026-08-07.
    """
    if not deploy:
        return (
            f"{service}: no Deployment or DaemonSet in this namespace "
            "(wrong name, wrong namespace, or the deploy never ran?)",
            1,
        )

    meta, status = deploy.get("metadata") or {}, deploy.get("status") or {}
    desired, updated, ready, available = _rollout_counts(deploy)
    # A spec edit bumps metadata.generation immediately; status.observedGeneration only catches
    # up once the controller has acted. Comparing them is what distinguishes "rolled out" from
    # "the controller has not looked at my change yet" — the old ReplicaSet satisfies every
    # replica count in the meantime.
    stale = status.get("observedGeneration", 0) < meta.get("generation", 0)
    rolled_out = (
        not stale and updated == desired and ready == desired and available == desired
    )

    restarts, recent = 0, []
    for pod in (pods or {}).get("items") or []:
        pod_name = (pod.get("metadata") or {}).get("name", "?")
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            count = cs.get("restartCount", 0)
            restarts += count
            if not count:
                continue
            finished = ((cs.get("lastState") or {}).get("terminated") or {}).get(
                "finishedAt"
            )
            age = _seconds_since(finished, now)
            where = f"{pod_name}/{cs.get('name', '?')}"
            # A restart whose time cannot be read counts as RECENT. Treating "unknown" as "long
            # ago" would fail open — the one direction a gate must never fail — and this branch
            # is reachable whenever kubectl's timestamp format shifts under us (every finishedAt
            # in this cluster is second-precision UTC today; fractional seconds parse as None).
            if age is None:
                recent.append(f"{where} restarted at an unreadable time ({finished!r})")
            elif age < RECENT_RESTART_SECONDS:
                recent.append(f"{where} restarted {int(age)}s ago")

    line = f"{service}: {ready}/{desired} ready, {updated} updated, restarts={restarts}"
    if stale:
        line += " — spec changed, rollout not observed yet"
    elif not rolled_out:
        line += " — rollout incomplete"
    if recent:
        line += f" — RECENT RESTART: {'; '.join(recent)}"
    return (line, 0 if rolled_out and not recent else 1)


def cert_stages(host, port, sni):
    """Two-stage pipeline: open a TLS session (with SNI) and decode the served
    leaf cert's subject/issuer/validity. Read-only — no data is sent.

    NB: connects to whatever DNS resolves `host` to. For a Cloudflare-proxied public host that's
    the CF edge (→ the Cloudflare edge cert), NOT Traefik's origin cert — pass the origin IP as the
    target with `--sni <host>` to inspect the origin Let's Encrypt cert."""
    s_client = [
        "openssl",
        "s_client",
        "-connect",
        f"{host}:{port}",
        "-servername",
        sni,
        "-verify_hostname",
        sni,
    ]
    x509 = [
        "openssl",
        "x509",
        "-noout",
        "-subject",
        "-issuer",
        "-dates",
        "-fingerprint",
        "-sha256",
    ]
    return [s_client, x509]


def format_metric(data):
    """Human view of a Prometheus /api/v1/query result. One `<labels> = <value>`
    line per series (labels are the metric dict minus __name__); a single
    label-less series prints just the value, so scalars read cleanly. A matrix
    (range vector) shows each series' latest point. Empty result -> 'no data'.

    Replaces the recurring `… | python3 -c "…[print(r['metric'].get('X'),'=',
    r['value'][1]) …]"` reshapes."""
    d = data.get("data") or {}
    result = d.get("result") or []
    if d.get("resultType") == "scalar":  # result = [ts, "val"]
        return str(result[1]) if len(result) == 2 else "no data"
    if not result:
        return "no data"
    lines = []
    for series in result:
        labels = {
            k: v for k, v in (series.get("metric") or {}).items() if k != "__name__"
        }
        key = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        if "value" in series:  # instant vector
            val = series["value"][1]
        else:  # matrix -> latest point
            vals = series.get("values") or []
            val = vals[-1][1] if vals else "?"
        lines.append(f"{key} = {val}" if key else str(val))
    return "\n".join(lines)


# Kuma's own numeric status codes, from the exporter that feeds monitor_status.
_MONITOR_STATUS_LABELS = {"0": "DOWN", "1": "UP", "2": "PENDING", "3": "MAINTENANCE"}


def format_monitor_status(data):
    """Format Kuma's monitor_status vector (Prometheus job=uptime-kuma) into a down-monitors
    rollup. Pure: takes the parsed instant-query response, returns (text, exit_code).

    Kuma keeps no history of its own — that's why `alerts` reconstructs the past from Loki
    instead of asking Kuma for it — but it does hold live state, and Prometheus already
    scrapes that state (postflight.py's check_kuma_monitors uses the same metric). No new
    Kuma API credential needed for "what's down right now" when this was already covering it.

    exit_code is 0 only when every monitor reports UP (1); PENDING and MAINTENANCE count as
    not-up too, same as DOWN, since neither means "confirmed healthy".
    """
    result = data.get("data", {}).get("result", [])
    if not result:
        return "no monitor_status series returned (uptime-kuma scrape target down?)", 1
    problems, up = [], 0
    for series in result:
        name = (series.get("metric") or {}).get("monitor_name", "?")
        status = (series.get("value") or [None, None])[1]
        if status == "1":
            up += 1
        else:
            problems.append(f"  {name}: {_MONITOR_STATUS_LABELS.get(status, status)}")
    summary = f"{up}/{len(result)} monitors up"
    if problems:
        return "\n".join([summary] + sorted(problems)), 1
    return summary, 0


def format_loki(data):
    """Human view of a Loki query_range result: just the log lines, sorted oldest
    -> newest across all streams (nanosecond-epoch timestamps), so the newest sits
    nearest the prompt. Empty result -> 'no logs'.

    Replaces the recurring `… | python3 -c "…for v in r['values']: print(v[1])"`."""
    rows = []
    for stream in (data.get("data") or {}).get("result") or []:
        for ts, line in stream.get("values") or []:
            rows.append((int(ts), line))
    if not rows:
        return "no logs"
    rows.sort(key=lambda r: r[0])
    return "\n".join(line for _, line in rows)


# --- alert history (pure) ---------------------------------------------------
# monitor-bridge is the homelab's alert brain: every INTERVAL it pushes each check's
# state to a Kuma push monitor and logs "[<ts>] DOWN <name> - <msg> (<n> cycles)" for
# any check that's firing. Kuma keeps only current state; Loki keeps the log lines
# (31d retention), so the history of *what alerted, when* is these DOWN lines. This
# collapses the every-cycle repeats into one row per firing episode.
ALERT_LOGQL = '{container="monitor-bridge"} |= "DOWN"'
_CHICAGO = ZoneInfo("America/Chicago")
# "[2026-07-21T08:37:00] DOWN n8n - 1 active workflow(s) failed ... (2 cycles)"
_DOWN_RE = re.compile(r"^\[[^\]]+\] DOWN (?P<name>\S+) - (?P<msg>.*)$")
_CYCLES_SUFFIX_RE = re.compile(r"\s*\(\d+ cycles?\)\s*$")


def parse_down_line(line):
    """(check_name, msg) for a monitor-bridge DOWN log line, else None. The
    trailing "(N cycles)" consecutive-down counter is stripped from msg."""
    m = _DOWN_RE.match(line)
    if not m:
        return None
    return m["name"], _CYCLES_SUFFIX_RE.sub("", m["msg"])


def alert_episodes(rows, gap_s=1800):
    """Collapse per-cycle DOWN samples into firing episodes.

    `rows` is an iterable of (epoch_ns, check_name, msg). Consecutive samples for the
    same check within `gap_s` seconds are one episode; a longer silence (the check
    recovered, then fired again) starts a new one. Returns episode dicts
    {name, first_ns, last_ns, cycles, msg} newest-episode-first (by last_ns). msg is
    the latest sample's — check messages evolve as the underlying value drifts."""
    by_name = defaultdict(list)
    for ns, name, msg in rows:
        by_name[name].append((int(ns), msg))
    episodes = []
    gap_ns = int(gap_s * 1e9)
    for name, samples in by_name.items():
        samples.sort()
        ep = None
        for ns, msg in samples:
            if ep is not None and ns - ep["last_ns"] <= gap_ns:
                ep["last_ns"] = ns
                ep["cycles"] += 1
                ep["msg"] = msg
            else:
                ep = {
                    "name": name,
                    "first_ns": ns,
                    "last_ns": ns,
                    "cycles": 1,
                    "msg": msg,
                }
                episodes.append(ep)
    episodes.sort(key=lambda e: e["last_ns"], reverse=True)
    return episodes


def _fmt_local(ns):
    return datetime.fromtimestamp(ns / 1e9, _CHICAGO).strftime("%Y-%m-%d %H:%M")


def _fmt_duration(ns):
    secs = ns / 1e9
    if secs < 60:
        return "1cyc"
    if secs < 3600:
        return f"{round(secs / 60)}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def format_alert_episodes(episodes, days):
    """Human view: one aligned row per episode, newest first (America/Chicago, the
    container-log timezone). Empty -> a clear all-clear line."""
    if not episodes:
        return f"no DOWN alerts in the last {days:g}d"
    width = max(len(e["name"]) for e in episodes)
    header = (
        f"{len(episodes)} DOWN episode(s), last {days:g}d (monitor-bridge -> Kuma):"
    )
    lines = [header, ""]
    for e in episodes:
        dur = _fmt_duration(e["last_ns"] - e["first_ns"])
        lines.append(
            f"{_fmt_local(e['first_ns'])}  {dur:>6}  "
            f"{e['name']:<{width}}  {e['cycles']:>3}c  {e['msg'][:88]}"
        )
    return "\n".join(lines)


# --- routing (pure given resolve_ip) ----------------------------------------


def _build_parser():
    p = argparse.ArgumentParser(
        prog="probe.py", description="read-only homelab diagnostics"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the command(s) instead of running"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metric", help="Prometheus instant query")
    m.add_argument("promql")
    m.add_argument(
        "--json",
        action="store_true",
        help="print raw JSON instead of the formatted view",
    )
    sub.add_parser("targets", help="Prometheus scrape-target health")
    sub.add_parser("monitors", help="Kuma down-monitors rollup (exit 0 = all up)")
    sub.add_parser("loki-labels", help="Loki label names")
    lq = sub.add_parser("loki-query", help="Loki range query")
    lq.add_argument("logql")
    lq.add_argument("--limit", type=int, default=100)
    lq.add_argument(
        "--since",
        help="how far back to look, e.g. 30m/6h/2d/1w. Loki defaults to ONE HOUR, and "
        "anything older simply returns nothing — an empty result reads as 'no logs' rather "
        "than 'you did not ask far enough back'.",
    )
    lq.add_argument(
        "--json", action="store_true", help="print raw JSON instead of the log lines"
    )
    al = sub.add_parser(
        "alerts", help="monitor-bridge DOWN alert history, collapsed to episodes (Loki)"
    )
    al.add_argument(
        "--days", type=float, default=7, help="lookback window in days (default 7)"
    )
    al.add_argument("--check", help="filter to check names containing this substring")
    al.add_argument(
        "--gap-min",
        type=float,
        default=30,
        help="minutes of silence that splits one episode from the next (default 30)",
    )
    al.add_argument("--limit", type=int, default=5000)
    al.add_argument(
        "--raw", action="store_true", help="print the matching log lines, not episodes"
    )
    al.add_argument(
        "--json", action="store_true", help="print episodes as JSON (epoch-ns times)"
    )
    sub.add_parser("scrutiny", help="disk SMART summary")
    b2l = sub.add_parser(
        "b2-longhorn",
        help="Longhorn backup objects in B2, per volume — proves DATA blocks landed, "
        "not just metadata (exit 1 if any volume has none)",
    )
    b2l.add_argument(
        "--bucket", help="override the bucket read from the kopia_b2_bucket secret"
    )
    b2l.add_argument(
        "--prefix", default=LONGHORN_PREFIX, help="B2 prefix Longhorn writes under"
    )
    b2b = sub.add_parser(
        "b2-budget",
        help="per-shard Class C projection against B2's free-tier daily cap "
        "(exit 1 if a weekly shard is over budget)",
    )
    b2b.add_argument(
        "--bucket", help="override the bucket read from the kopia_b2_bucket secret"
    )
    b2b.add_argument(
        "--prefix", default=LONGHORN_PREFIX, help="B2 prefix Longhorn writes under"
    )
    b2b.add_argument(
        "--retain",
        type=int,
        default=2,
        help="k3s_longhorn_weekly_backup_retain, to spot backups no job will prune",
    )
    b2s = sub.add_parser(
        "b2-spend",
        help="MEASURED Class B spend from Longhorn's own logs (b2-budget projects Class C; "
        "this counts what backups actually cost). Reads Loki, spends nothing on B2.",
    )
    b2s.add_argument("--since", default="24h", help="window, e.g. 30m/6h/2d/1w")
    b2s.add_argument("--limit", type=int, default=1000)
    b2r = sub.add_parser(
        "b2-record",
        help="record a tool's B2 transaction spend in today's ledger, so maintenance "
        "spend stops being reconstructed from memory",
    )
    b2r.add_argument(
        "--tool", required=True, help="what spent them, e.g. drain, inventory"
    )
    b2r.add_argument("--class-a", type=int, default=0, dest="class_a")
    b2r.add_argument("--class-b", type=int, default=0, dest="class_b")
    b2r.add_argument("--class-c", type=int, default=0, dest="class_c")
    b2r.add_argument("--note", default="")
    pi = sub.add_parser("pi", help="Pi glances API")
    pi.add_argument("subpath", help="e.g. fs, quicklook, mem, cpu")
    ct = sub.add_parser(
        "cert",
        help="served TLS cert details (public hosts show the CF edge cert — "
        "pass the origin IP + --sni for the origin cert)",
    )
    ct.add_argument("target", help="host or host:port")
    ct.add_argument("--sni", help="SNI servername (defaults to host)")
    hl = sub.add_parser(
        "health", help="k8s rollout + restart rollup (exit 0 = healthy)"
    )
    hl.add_argument("container", help="service name, e.g. jellyfin")
    hl.add_argument(
        "--docker",
        action="store_true",
        help="inspect the Pi's Docker container instead of a k8s Deployment",
    )
    ar = sub.add_parser(
        "arr", help="read-only *arr API GET (key from SOPS, fed via stdin)"
    )
    ar.add_argument("app", choices=sorted(ARR_PORTS))
    ar.add_argument("path", help="api path, e.g. health, indexerstatus, notification")
    ar.add_argument(
        "--json", action="store_true", help="print raw JSON instead of pretty-printed"
    )
    ha = sub.add_parser("ha", help="Home Assistant live state (read-only, GET)")
    hasub = ha.add_subparsers(dest="ha_cmd", required=True)
    hs = hasub.add_parser("state", help="GET /api/states/<entity_id>")
    hs.add_argument("entity_id", help="e.g. fan.tower_fan")
    hs.add_argument("--json", action="store_true", help="print raw JSON")
    hauto = hasub.add_parser(
        "automation", help="one automation by id, alias-slug, or entity_id"
    )
    hauto.add_argument(
        "query", help="automation id, alias-slug, or full automation.<slug>"
    )
    hauto.add_argument("--json", action="store_true", help="print raw JSON")
    hg = hasub.add_parser("get", help="raw GET /api/<path>, e.g. error_log")
    hg.add_argument("path")
    htr = hasub.add_parser(
        "trace",
        aliases=["why"],
        help="why an automation last ran/no-op'd (per-condition WS trace)",
    )
    htr.add_argument(
        "query", help="automation id, alias-slug, or full automation.<slug>"
    )
    hasub.add_parser(
        "verify-automations",
        help="assert every automation in automations.yaml loaded (exit 0 = all loaded)",
    )
    hasub.add_parser(
        "verify-entities",
        help="assert every entity in state/external_entities.yml still exists live",
    )
    hst = sub.add_parser("ha-state", help="live view of the derived state model")
    hst.add_argument(
        "--inventory",
        action="store_true",
        help="also dump every live entity grouped by domain",
    )
    return p


def plan(args, resolve_ip, k8s_endpoint=k8s_endpoint, pi_resolve=pi_resolve):
    """Return the command pipeline (list of argv stages) for the parsed args.

    `resolve_ip(container) -> ip`, `k8s_endpoint(hostname) -> (base, pin)`, and
    `pi_resolve() -> pin` are injected so all routing/URL logic is testable without
    Docker, SOPS, or the network. Most commands are a single stage; `cert` is a
    two-stage openssl pipeline.
    """
    ns = _build_parser().parse_args(args)
    cmd = ns.cmd
    if cmd == "metric":
        base, pin = k8s_endpoint("prometheus")
        return [curl_argv(prom_query_url(base, ns.promql), resolve=pin)]
    if cmd == "targets":
        base, pin = k8s_endpoint("prometheus")
        return [curl_argv(prom_targets_url(base), resolve=pin)]
    if cmd == "loki-labels":
        base, pin = k8s_endpoint("loki-homelab")
        return [curl_argv(loki_labels_url(base), resolve=pin)]
    if cmd == "loki-query":
        base, pin = k8s_endpoint("loki-homelab")
        start = end = None
        if getattr(ns, "since", None):
            end_s = datetime.now(_CHICAGO).timestamp()
            start = int((end_s - parse_duration_seconds(ns.since)) * 1e9)
            end = int(end_s * 1e9)
        return [
            curl_argv(
                loki_query_url(base, ns.logql, ns.limit, start=start, end=end),
                resolve=pin,
            )
        ]
    if cmd == "scrutiny":
        base, pin = k8s_endpoint("scrutiny")
        return [curl_argv(scrutiny_url(base), resolve=pin)]
    if cmd == "pi":
        # daniel-pi.lan is a Pi-hole-only LAN name; this host's resolver bypasses it (same
        # trap as every other cluster/LAN name here), so pin it like k8s_endpoint does.
        return [curl_argv(pi_url(ns.subpath), resolve=pi_resolve())]
    if cmd == "cert":
        host, _, port = ns.target.partition(":")
        port = int(port) if port else 443
        return cert_stages(host, port, ns.sni or host)
    raise SystemExit(f"unknown command: {cmd}")  # pragma: no cover


# --- runtime (impure) -------------------------------------------------------


def resolve_ip(container):
    out = subprocess.run(inspect_ip_argv(container), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"docker inspect {container} failed: {out.stderr.strip()}")
    ip = parse_ip(out.stdout)
    if not ip:
        raise SystemExit(f"{container} has no container IP (is it running?)")
    return ip


def resolve_arr_ip(app):
    """The *arr app's k8s Service ClusterIP — resolve_ip's k8s equivalent, used instead of
    k8s_endpoint because sonarr/radarr/prowlarr have no Authelia bypass rule for /api/* (see
    the comment above ARR_PORTS). A ClusterIP is stable across pod restarts and redeploys,
    so this doesn't reintroduce the hand-copied-IP staleness `docker inspect` was resolving
    around in the first place.

    CAVEAT confirmed live 2026-08-17: this only reaches the app when its pod is scheduled on
    THIS node (daniel-box). Each app's NetworkPolicy allows ingress only from specific pod
    selectors, no ipBlock for the host — sonarr/radarr (on daniel-box) answered anyway, but
    prowlarr (on daniel-server that day) refused the connection although ICMP to its pod IP
    got through, so this is the NetworkPolicy's enforcement, not routing. Host-originated
    traffic apparently doesn't pass through the destination node's own NetworkPolicy iptables
    the same way same-node traffic does. This will flip on the next reschedule; a real fix
    needs a NetworkPolicy ipBlock for the node (ansible/roles/k8s/*/templates/), out of scope
    here."""
    ns = k8s_namespace()
    out = subprocess.run(k8s_service_ip_argv(app, ns), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"kubectl get service {app} failed: {out.stderr.strip()}")
    ip = out.stdout.strip()
    if not ip:
        raise SystemExit(f"{app} has no ClusterIP (does the Service exist?)")
    return ip


def run_pipeline(stages):
    """Run argv stages piped together (stdin of the first is closed)."""
    prev = subprocess.DEVNULL
    procs = []
    for i, stage in enumerate(stages):
        last = i == len(stages) - 1
        proc = subprocess.Popen(
            stage,
            stdin=prev,
            stdout=None if last else subprocess.PIPE,
            stderr=subprocess.DEVNULL if not last else None,
        )
        if prev not in (subprocess.DEVNULL, None):
            prev.close()
        prev = proc.stdout
        procs.append(proc)
    return procs[-1].wait()


def fetch(url, resolve=None):
    """Run the read-only curl GET and return its body (raise on failure)."""
    out = subprocess.run(
        curl_argv(url, resolve=resolve), capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SystemExit(f"curl {url} failed: {out.stderr.strip()}")
    return out.stdout


def run_query(ns):
    """Fetch a metric / loki-query and print the formatted view (the default).
    `--json` and `--dry-run` never reach here — they take the raw streaming path."""
    if ns.cmd == "metric":
        base, pin = prom_endpoint()
        url = prom_query_url(base, ns.promql)
        formatter = format_metric
    else:
        base, pin = loki_endpoint()
        url = loki_query_url(base, ns.logql, ns.limit)
        formatter = format_loki
    body = fetch(url, resolve=pin)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(body.strip())
        return 1
    print(formatter(data))
    return 0


def run_monitors(ns):
    """Print Kuma's live down-monitors rollup (exit 0 = all up)."""
    base, pin = prom_endpoint()
    url = prom_query_url(base, "monitor_status")
    if ns.dry_run:
        print(" ".join(curl_argv(url, resolve=pin)))
        return 0
    body = fetch(url, resolve=pin)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(body.strip())
        return 1
    text, code = format_monitor_status(data)
    print(text)
    return code


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


def run_alerts(ns):
    """Fetch monitor-bridge's DOWN log lines over the window and print firing episodes."""
    end_s = datetime.now(_CHICAGO).timestamp()
    start_s = end_s - ns.days * 86400
    base, pin = loki_endpoint()
    url = loki_query_url(
        base,
        ALERT_LOGQL,
        ns.limit,
        start=int(start_s * 1e9),
        end=int(end_s * 1e9),
        direction="forward",
    )
    if ns.dry_run:
        print(" ".join(curl_argv(url, resolve=pin)))
        return 0
    raw = _rows_from_loki(json.loads(fetch(url, resolve=pin)))
    if ns.raw:
        print("\n".join(line for _, line in raw) or "no logs")
    else:
        rows = []
        for ns_ts, line in raw:
            parsed = parse_down_line(line)
            if parsed is None:
                continue
            name, msg = parsed
            if ns.check and ns.check.lower() not in name.lower():
                continue
            rows.append((ns_ts, name, msg))
        episodes = alert_episodes(rows, ns.gap_min * 60)
        if ns.json:
            print(json.dumps(episodes, indent=2))
        else:
            print(format_alert_episodes(episodes, ns.days))
    if len(raw) >= ns.limit:
        print(
            f"\n(warning: hit --limit {ns.limit} log lines — results may be truncated; "
            "raise --limit or narrow --days)"
        )
    return 0


def k8s_namespace():
    """The workload namespace, read from inventory (plaintext, not a secret)."""
    with open(GROUP_VARS_PATH) as f:
        for line in f:
            if line.startswith("k8s_namespace:"):
                return line.split(":", 1)[1].strip()
    raise SystemExit(f"k8s_namespace not found in {GROUP_VARS_PATH}")


def _json_or_none(argv):
    out = subprocess.run(argv, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def run_health(container, docker=False):
    """k8s Deployment health by default, the Pi's Docker container with --docker.

    k8s first because that is where ~50 of the ~55 services live since the 2026-08-14 Docker
    retirement. Before that this command ran `docker inspect` unconditionally and had been
    dead on both cluster nodes ever since — it died with `FileNotFoundError: 'docker'`,
    because neither node has the binary at all.
    """
    if docker:
        # daniel-pi is the only Docker host left, and probe.py runs on daniel-box, so this is
        # necessarily remote. The ssh is internal to the script, so it is covered by probe.py's
        # own allow-list entry and never reaches the Bash classifier.
        argv = ["ssh", PI_HOST] + inspect_argv(container)
        out = subprocess.run(argv, capture_output=True, text=True)
        try:
            data = json.loads(out.stdout) if out.returncode == 0 else []
        except json.JSONDecodeError:
            data = []
        text, code = format_health(data, container)
        print(text)
        return code

    ns = k8s_namespace()
    # Deployment first, DaemonSet second — the fleet is overwhelmingly Deployments, and asking
    # for the wrong kind just returns non-zero, so the fallback costs one extra call only for
    # the six DaemonSets and for a name that matches neither.
    deploy = _json_or_none(k8s_deploy_argv(container, ns)) or _json_or_none(
        k8s_deploy_argv(container, ns, kind="daemonset")
    )
    pods = _json_or_none(k8s_pods_argv(container, ns)) if deploy else None
    text, code = format_k8s_health(deploy, pods, container, datetime.now(timezone.utc))
    print(text)
    return code


def ha_token():
    """Decrypt claude_ha_token from the SOPS secrets file. Requires the host's age
    key (present on daniel-server, where HA runs)."""
    return sops_extract("claude_ha_token")


def ha_get(url, token, resolve=None):
    """Authenticated HA GET; returns the response body. Token is passed via stdin."""
    return config_get(url, ha_curl_config(token), resolve=resolve)


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


def run_arr(ns):
    """Read-only *arr API GET, resolved to the app's k8s Service ClusterIP.

    sonarr/radarr/prowlarr have run as k8s Deployments since 2026-08-07 (B4c) and have
    no Docker container left to `docker inspect` an IP from — this used to shell out to
    `resolve_ip(ns.app)`, which died with `FileNotFoundError: 'docker'` on both cluster
    nodes. resolve_arr_ip replaces it with the same idea (resolve the current address at
    run time) via kubectl instead of docker — see the comment above ARR_PORTS for why
    this talks to the Service directly instead of going through k8s_endpoint like every
    other cluster subcommand. Pulls <app>_api_key from SOPS and passes it via stdin.
    Pretty-prints JSON by default; `--json` prints the raw response."""
    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(arr_url("<arr-clusterip>", ns.app, ns.path)))
            + "   # + X-Api-Key: <redacted> (via --config stdin)"
        )
        return 0
    url = arr_url(resolve_arr_ip(ns.app), ns.app, ns.path)
    body = config_get(url, arr_curl_config(sops_extract(f"{ns.app}_api_key")))
    if ns.json:
        print(body, end="")
        return 0
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body.strip())
        return 1
    return 0


def _ha_url(ip, ns):
    if ns.ha_cmd == "state":
        return ha_state_url(ip, ns.entity_id)
    if ns.ha_cmd == "automation":
        return ha_get_url(ip, "states")  # fetch all, then match locally
    return ha_get_url(ip, ns.path)  # get


def run_ha(ns):
    if ns.ha_cmd in ("trace", "why"):
        if ns.dry_run:
            print(
                f"wss://<ha-bridge-host>/api/websocket  trace/list+trace/get for {ns.query!r} "
                f"# + auth Bearer <redacted>"
            )
            return 0
        token = ha_token()
        states = json.loads(
            ha_get(ha_get_url(ha_base(), "states"), token, resolve=ha_resolve())
        )
        m = match_automation(states, ns.query)
        if m is None:
            print(
                f"automation '{ns.query}' not found (by entity_id, id, or alias-slug)"
            )
            return 1
        automation_id = m.get("attributes", {}).get("id")
        if not automation_id:
            print(f"{m['entity_id']}: no config id (cannot fetch trace)")
            return 1
        print(
            format_trace(
                ha_trace(ha_host(), token, automation_id, connect_ip=metallb_vip())
            )
        )
        return 0
    if ns.ha_cmd == "verify-automations":
        if ns.dry_run:
            print(
                " ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states")))
                + f"   # + Bearer; compare attributes.id against ids in {AUTOMATIONS_YAML}"
            )
            return 0
        states = json.loads(
            ha_get(ha_get_url(ha_base(), "states"), ha_token(), resolve=ha_resolve())
        )
        live = [s for s in states if s.get("entity_id", "").startswith("automation.")]
        with open(AUTOMATIONS_YAML, encoding="utf-8") as f:
            expected = expected_automation_ids(f.read())
        errs = automation_load_errors(expected, live)
        if errs:
            for e in errs:
                print(e)
            return 1
        print(f"all {len(expected)} automations loaded")
        return 0
    if ns.ha_cmd == "verify-entities":
        if ns.dry_run:
            print(
                " ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states")))
                + f"   # + Bearer; compare live entity_ids against {EXTERNAL_ENTITIES_YAML}"
            )
            return 0
        states = json.loads(
            ha_get(ha_get_url(ha_base(), "states"), ha_token(), resolve=ha_resolve())
        )
        with open(EXTERNAL_ENTITIES_YAML, encoding="utf-8") as f:
            snapshot = snapshot_entity_ids(f.read())
        gone = vanished_snapshot_entities(
            snapshot, [s.get("entity_id", "") for s in states]
        )
        if gone:
            for e in gone:
                print(f"{e} is in external_entities.yml but no longer exists live")
            print(
                f"{len(gone)} vanished; re-point the config that reads them, then run "
                "`ha_state_model.py refresh` + `generate`"
            )
            return 1
        print(f"all {len(snapshot)} snapshot entities still exist")
        return 0
    if ns.dry_run:
        argv = ha_curl_argv(_ha_url("<ha-ip>", ns))
        print(
            " ".join(argv)
            + "   # + Authorization: Bearer <redacted> (via --config stdin)"
        )
        return 0
    body = ha_get(_ha_url(ha_base(), ns), ha_token(), resolve=ha_resolve())
    if ns.ha_cmd == "get":
        print(body, end="")
        return 0
    if ns.ha_cmd == "state":
        if ns.json:
            print(body)
            return 0
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            print(body.strip())
            return 1
        # A missing entity returns {"message": "Entity not found."}.
        if not isinstance(obj, dict) or "entity_id" not in obj:
            msg = obj.get("message") if isinstance(obj, dict) else body.strip()
            print(f"{ns.entity_id}: {msg or 'not found'}")
            return 1
        print(format_ha_state(obj))
        return 0
    # automation
    m = match_automation(json.loads(body), ns.query)
    if m is None:
        print(f"automation '{ns.query}' not found (by entity_id, id, or alias-slug)")
        return 1
    print(json.dumps(m, indent=2) if ns.json else format_ha_automation(m))
    return 0


def run_ha_state(ns):
    import json
    import ha_state_model

    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states")))
            + "   # + Bearer (stdin)"
        )
        return 0
    body = ha_get(ha_get_url(ha_base(), "states"), ha_token(), resolve=ha_resolve())
    states = json.loads(body)
    model = ha_state_model.build_model(ha_state_model.load_role())
    print(ha_state_rows(states, model))
    if ns.inventory:
        print("\nInventory:")
        for s in sorted(states, key=lambda x: x["entity_id"]):
            print(f"  {s['entity_id']:<55} {s['state']}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ns = _build_parser().parse_args(argv)
    # `health` parses/formats docker inspect rather than streaming a pipeline.
    if ns.cmd == "health":
        if ns.dry_run:
            if ns.docker:
                print(" ".join(["ssh", PI_HOST] + inspect_argv(ns.container)))
            else:
                ns_name = k8s_namespace()
                print(" ".join(k8s_deploy_argv(ns.container, ns_name)))
                print(
                    " ".join(k8s_deploy_argv(ns.container, ns_name, kind="daemonset"))
                    + "   # only if the Deployment lookup misses"
                )
                print(" ".join(k8s_pods_argv(ns.container, ns_name)))
            return 0
        return run_health(ns.container, docker=ns.docker)
    # `ha` resolves a token + talks to the HA REST API rather than streaming a pipeline.
    if ns.cmd == "ha":
        return run_ha(ns)
    if ns.cmd == "arr":
        return run_arr(ns)
    if ns.cmd == "alerts":
        return run_alerts(ns)
    if ns.cmd == "b2-longhorn":
        return run_b2_longhorn(ns)
    if ns.cmd == "b2-budget":
        return run_b2_budget(ns)
    if ns.cmd == "b2-spend":
        return run_b2_spend(ns)
    if ns.cmd == "b2-record":
        return run_b2_record(ns)
    if ns.cmd == "ha-state":
        return run_ha_state(ns)
    if ns.cmd == "monitors":
        return run_monitors(ns)
    # metric / loki-query default to a formatted view; --json and --dry-run fall
    # through to the raw streaming path below.
    if ns.cmd in ("metric", "loki-query") and not ns.json and not ns.dry_run:
        return run_query(ns)
    stages = plan(argv, resolve_ip)
    if ns.dry_run:
        for stage in stages:
            print(" ".join(stage))
        return 0
    return run_pipeline(stages)


# --- B2 / Longhorn backup objects -------------------------------------------

LONGHORN_PREFIX = "longhorn"
B2_API_VERSION = "v3"
B2_AUTHORIZE_URL = (
    f"https://api.backblazeb2.com/b2api/{B2_API_VERSION}/b2_authorize_account"
)


# This used to run `rclone lsf` inside the kopia container. Both halves of that are gone:
# the k3s migration removed Docker from daniel-box and daniel-server (2026-08-14), and kopia
# itself was retired — its `kopia_b2_*` secrets are Longhorn's credentials now. The command
# therefore died with `FileNotFoundError: 'docker'` on every real run, while the tests kept
# passing because they only ever exercised the argv builder and the parser. B2's native API
# needs no SigV4 signing, so plain curl replaces both dependencies.
def b2_curl(config_body, timeout=DEFAULT_TIMEOUT):
    """One B2 API call, with url and credentials fed through curl's stdin config.

    Same guard as the HA and *arr helpers above: neither the application key nor the
    session token may appear in argv, where `ps` would expose them to any local user.
    """
    out = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "--config", "-"],
        input=config_body,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit("B2 request failed: " + out.stderr.strip()[:400])
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        # B2 reports a refused transaction cap as a JSON error, so a non-JSON body here
        # is a different problem (proxy, DNS, truncation) and is worth showing verbatim.
        raise SystemExit("B2 returned a non-JSON body: " + out.stdout.strip()[:200])


def b2_authorize_config(key_id, app_key):
    return f'url = "{B2_AUTHORIZE_URL}"\nuser = "{key_id}:{app_key}"\n'


def b2_list_files_config(api_url, token, bucket_id, prefix, start=None):
    query = {
        "bucketId": bucket_id,
        "prefix": prefix.rstrip("/") + "/",
        "maxFileCount": "1000",
    }
    if start:
        query["startFileName"] = start
    url = f"{api_url}/b2api/{B2_API_VERSION}/b2_list_file_names?{urlencode(query)}"
    return f'url = "{url}"\nheader = "Authorization: {token}"\n'


def b2_list_buckets_config(api_url, token, account_id, bucket_name):
    query = {"accountId": account_id, "bucketName": bucket_name}
    url = f"{api_url}/b2api/{B2_API_VERSION}/b2_list_buckets?{urlencode(query)}"
    return f'url = "{url}"\nheader = "Authorization: {token}"\n'


def b2_longhorn_lines(
    key_id, app_key, bucket, prefix=LONGHORN_PREFIX, _call=b2_curl, _stats=None
):
    """List the Longhorn prefix, returning `path;size` lines.

    The shape is rclone's `lsf --format ps --separator ;` verbatim, and the paths are made
    relative to the prefix the same way rclone's did — so parse_longhorn_listing below is
    unchanged and its tests still describe the real input. Leaving the paths absolute would
    match none of its patterns and report a healthy bucket as "no Longhorn backup objects".
    """
    auth = _call(b2_authorize_config(key_id, app_key))
    storage = auth.get("apiInfo", {}).get("storageApi", {})
    api_url, token = storage.get("apiUrl"), auth.get("authorizationToken")
    if not api_url or not token:
        raise SystemExit("B2 authorize returned no apiUrl/authorizationToken")

    # A bucket-scoped application key already names its bucket; an account-wide one does not
    # and has to be looked up.
    bucket_id = storage.get("bucketId")
    if not bucket_id:
        listed = _call(
            b2_list_buckets_config(api_url, token, auth.get("accountId", ""), bucket)
        )
        buckets = listed.get("buckets", [])
        if not buckets:
            raise SystemExit(f"B2 has no bucket named {bucket}")
        bucket_id = buckets[0]["bucketId"]

    strip = prefix.rstrip("/") + "/"
    lines, start = [], None
    pages = 0
    while True:
        page = _call(b2_list_files_config(api_url, token, bucket_id, prefix, start))
        pages += 1
        for entry in page.get("files", []):
            name = entry.get("fileName", "")
            if name.startswith(strip):
                name = name[len(strip) :]
            lines.append(f"{name};{entry.get('contentLength', 0)}")
        start = page.get("nextFileName")
        if not start:
            # Each page is one b2_list_file_names, and the authorize that preceded them is
            # itself billable — both Class C. Reported through an out-param so the existing
            # callers and their tests keep the plain list return.
            if _stats is not None:
                _stats["class_c"] = pages + 1
                _stats["pages"] = pages
            return lines


def parse_longhorn_listing(lines):
    """Aggregate `rclone lsf --format ps` output per Longhorn volume.

    Longhorn lays a backup out as
    `backupstore/volumes/<aa>/<bb>/<volume>/{volume.cfg,backups/*.cfg,blocks/**/*.blk}`.
    The `.blk` files are the actual DATA; the `.cfg` files are only metadata, and that
    distinction is the entire point of this tool — a backup can be registered and report
    `Completed` in Longhorn while what actually landed in B2 is metadata describing blocks
    that are not there. Counting blocks is what makes "the data really is in B2" checkable.
    """
    vols = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        path, _, size = line.rpartition(";")
        if not path:
            continue
        parts = path.split("/")
        if "volumes" not in parts:
            continue
        i = parts.index("volumes")
        if len(parts) < i + 4:  # volumes/<aa>/<bb>/<volume>/...
            continue
        v = vols.setdefault(parts[i + 3], {"blocks": 0, "block_bytes": 0, "cfgs": 0})
        try:
            nbytes = int(size)
        except ValueError:
            nbytes = 0
        if path.endswith(".blk"):
            v["blocks"] += 1
            v["block_bytes"] += nbytes
        elif path.endswith(".cfg"):
            v["cfgs"] += 1
    return vols


def format_longhorn_summary(vols):
    """Render the per-volume table; non-zero exit if any volume has metadata but no data."""
    if not vols:
        return "no Longhorn backup objects found under the prefix", 1
    width = max(len(n) for n in vols)
    rows, bad = [], []
    for name in sorted(vols):
        v = vols[name]
        rows.append(
            "%-*s  %6d blocks  %8.1f MB  %3d cfg"
            % (width, name, v["blocks"], v["block_bytes"] / 1e6, v["cfgs"])
        )
        if v["blocks"] == 0:
            bad.append(name)
    out = "\n".join(rows)
    if bad:
        out += "\n\nNO DATA BLOCKS for: %s — metadata only, not restorable" % ", ".join(
            bad
        )
    return out, (1 if bad else 0)


# B2's free tier allows 2,500 Class C transactions a day. Longhorn's retention delete is the
# thing that spends them: DeleteDeltaBlockBackup walks the volume's whole block tree with one
# delimited ListObjects per directory (backupstore deltablock.go getBlockNamesForVolume), and it
# runs once per deleted backup. So a shard's daily Class C cost is set by how many BLOCKS its
# volumes hold, not by how much data changed — which is why this is worth watching as volumes
# grow, and why the seventh cap event was not preventable by looking at bytes.
B2_CLASS_C_DAILY_CAP = 2500
# Headroom for everything this model does not count: monitor-bridge's two B2 probes
# (check_b2_reachable, and check_b2_storage's daily listing), and the per-prune extras beyond the
# block walk — the deletion lock check, the backups/ name list, and one cfg GET per retained
# backup. NOT kopia: it was retired with the k3s migration and issues no B2 traffic at all. Only
# the `kopia_b2_*` SOPS key names survive, and they are Longhorn's credentials now.
B2_BUDGET_RESERVE = 400


def parse_backup_budget(lines):
    """Per-volume backup count, block count, and the Class C cost of one retention prune.

    Takes the same `path;size` lines as parse_longhorn_listing. A prune costs one
    ListObjects for `blocks/`, one per first-level hash directory, and one per second-level
    directory — so the DIRECTORY counts, not the block count, are the price.
    """
    vols = {}
    for line in lines:
        path = line.strip().rpartition(";")[0]
        if not path:
            continue
        parts = path.split("/")
        if "volumes" not in parts:
            continue
        i = parts.index("volumes")
        if len(parts) < i + 4:
            continue
        v = vols.setdefault(
            parts[i + 3], {"backups": 0, "blocks": 0, "lv1": set(), "lv2": set()}
        )
        tail = parts[i + 4 :]
        if tail[:1] == ["backups"] and path.endswith(".cfg"):
            v["backups"] += 1
        elif path.endswith(".blk") and tail[:1] == ["blocks"] and len(tail) >= 4:
            v["blocks"] += 1
            v["lv1"].add(tail[1])
            v["lv2"].add((tail[1], tail[2]))
    for v in vols.values():
        v["prune"] = 1 + len(v["lv1"]) + len(v["lv2"])
    return vols


def format_backup_budget(vols, shards, names=None, retain=2):
    """Render the per-shard Class C projection; non-zero exit if a shard is over budget.

    `shards` maps volume name to its recurring-job group. A volume with no group never runs a
    backup and never prunes, so it is reported separately rather than charged to a day.
    """
    names = names or {}
    budget = B2_CLASS_C_DAILY_CAP - B2_BUDGET_RESERVE
    byshard, idle, daily = {}, [], []
    for vol, v in vols.items():
        shard = shards.get(vol)
        if shard and shard.startswith("weekly-backup-"):
            byshard.setdefault(shard, []).append((vol, v))
        elif shard in (None, "no-backup"):
            idle.append(vol)
        else:
            # A PVC provisioned from the `longhorn` StorageClass lands in `default` — the DAILY
            # group — until the next deploy reconciles its label. On B2 that is a prune every
            # night against a budget sized for one a week, so it is the loudest thing here.
            daily.append(vol)

    rows, over = [], []
    for shard in sorted(byshard):
        members = sorted(byshard[shard], key=lambda kv: -kv[1]["prune"])
        total = sum(v["prune"] for _, v in members)
        # Backups beyond `retain` are STRANDED, not queued for deletion. Longhorn enforces
        # retain only when the owning RecurringJob runs against a volume still in its `groups:`,
        # and it counts only ITS OWN backups — so the daily-era backups on a volume that moved to
        # a weekday shard are pruned by nothing, ever. Measured 2026-08-17: radarr-config sat at
        # 4 daily-backup + 1 weekly-backup-d2 against retain 4 and deleted none, because the
        # weekly job saw 1 of its own. `longhorn-reap-orphan-backups.sh` is what clears these.
        #
        # The consequence for this projection: a shard's prune cost does not begin until that
        # job has more than `retain` of its own backups, and until then its blocks only grow.
        stranded = sum(max(0, v["backups"] - retain) for _, v in members)
        flag = "OVER" if total > budget else "ok"
        if total > budget:
            over.append(shard)
        rows.append(
            "%-17s %5d C  %s%s"
            % (
                shard,
                total,
                flag,
                "  (%d stranded backup(s) — see the orphan-backup reaper)" % stranded
                if stranded
                else "",
            )
        )
        for vol, v in members:
            rows.append(
                "    %-22s %5d C  %5d blocks  %2d backups"
                % (names.get(vol, vol)[:22], v["prune"], v["blocks"], v["backups"])
            )
    if idle:
        rows.append(
            "no-backup (never pruned): %s"
            % ", ".join(sorted(names.get(v, v) for v in idle))
        )
    if daily:
        over.append("daily-on-B2")
        rows.append(
            "ON THE DAILY TIER AND ON B2: %s — %d C every night, not once a week. "
            "Route to r2 or move to a weekly shard."
            % (
                ", ".join(sorted(names.get(v, v) for v in daily)),
                sum(vols[v]["prune"] for v in daily),
            )
        )
    rows.append(
        "budget per day: %d Class C (cap %d less %d reserved for the B2 probes and prune overhead)"
        % (budget, B2_CLASS_C_DAILY_CAP, B2_BUDGET_RESERVE)
    )
    if over:
        rows.append("OVER BUDGET: %s — rebalance before the next run" % ", ".join(over))
    return "\n".join(rows), (1 if over else 0)


def volume_shard_labels(_run=None):
    """{longhorn volume: recurring-job group} from the live cluster."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "volumes.longhorn.io",
            "-o",
            "json",
        ]
    )
    if out.returncode != 0:
        raise SystemExit("kubectl failed: " + out.stderr.strip()[:300])
    shards = {}
    for item in json.loads(out.stdout).get("items", []):
        for key in item.get("metadata", {}).get("labels", {}):
            if key.startswith("recurring-job-group.longhorn.io/"):
                shards[item["metadata"]["name"]] = key.split("/", 1)[1]
    return shards


def pvc_names(_run=None):
    """{longhorn volume: PVC name}, so the report reads in service terms."""
    run = _run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    out = run(["kubectl", "get", "pv", "-o", "json"])
    if out.returncode != 0:
        return {}
    return {
        pv["metadata"]["name"]: pv["spec"].get("claimRef", {}).get("name", "")
        for pv in json.loads(out.stdout).get("items", [])
        if pv.get("spec", {}).get("claimRef")
    }


def run_b2_spend(ns):
    """Measured Class B spend from Longhorn's own logs, over --since.

    Costs nothing against B2 — it reads Loki, not the backup store. This is the counterpart to
    b2-budget: that one PROJECTS Class C from block counts, this one MEASURES Class B from what
    the backups actually did.
    """
    seconds = parse_duration_seconds(ns.since)
    end_s = datetime.now(_CHICAGO).timestamp()
    base, pin = loki_endpoint()
    url = loki_query_url(
        base,
        SPEND_LOGQL,
        ns.limit,
        start=int((end_s - seconds) * 1e9),
        end=int(end_s * 1e9),
        direction="forward",
    )
    if ns.dry_run:
        print(" ".join(curl_argv(url, resolve=pin)))
        return 0
    rows = _rows_from_loki(json.loads(fetch(url, resolve=pin)))
    # Reads Loki, not B2 — nothing to record for this command itself.
    print(
        format_backup_spend(
            parse_backup_spend(rows), ns.since, pvc_names(), read_b2_ledger()
        )
    )
    return 0


def run_b2_record(ns):
    """Record a tool's B2 spend in today's ledger.

    Exists so scripts outside this repo — the one-shot drains and inventories an operator writes
    during an incident — can contribute to the same tally instead of scrolling past in a terminal
    and being reconstructed from memory afterwards.
    """
    record_b2_spend(ns.tool, ns.class_a, ns.class_b, ns.class_c, ns.note)
    print(
        "recorded %s: %d Class A, %d Class B, %d Class C -> %s"
        % (ns.tool, ns.class_a, ns.class_b, ns.class_c, b2_ledger_path())
    )
    return 0


def run_b2_budget(ns):
    """Project each weekly shard's Class C spend against B2's free-tier daily cap.

    One listing (~10 Class C), so it is cheap enough to run weekly and far too expensive to
    put in the 10-minute monitor cron.
    """
    if ns.dry_run:
        print(
            f"GET {B2_AUTHORIZE_URL} then b2_list_file_names prefix={ns.prefix.rstrip('/')}/ "
            "plus kubectl get volumes.longhorn.io,pv"
        )
        return 0

    stats = {}
    lines = b2_longhorn_lines(
        sops_extract("kopia_b2_key_id"),
        sops_extract("kopia_b2_application_key"),
        ns.bucket or sops_extract("kopia_b2_bucket"),
        ns.prefix,
        _stats=stats,
    )
    record_b2_spend(
        "b2-budget",
        class_c=stats.get("class_c", 0),
        note=f"{stats.get('pages', 0)} pages",
    )
    text, code = format_backup_budget(
        parse_backup_budget(lines), volume_shard_labels(), pvc_names(), ns.retain
    )
    print(text)
    return code


def run_b2_longhorn(ns):
    """Prove Longhorn's backups hold real data blocks in B2, not just metadata.

    The design doc's §6 gate is that a service's data must be visible in its NEW backup
    path before the Docker copy is decommissioned, so slice 2 needs this per service.

    Costs a handful of transactions (one listing, paged at 1000 objects) — negligible
    against the daily free allowance, but not free: don't put it in a loop.
    """
    # Ahead of the credential read: --dry-run exists to describe the call WITHOUT making it,
    # so it must not decrypt anything either.
    if ns.dry_run:
        bucket = ns.bucket or "<kopia_b2_bucket>"
        print(
            f"GET {B2_AUTHORIZE_URL} then b2_list_file_names "
            f"bucket={bucket} prefix={ns.prefix.rstrip('/')}/"
        )
        return 0

    bucket = ns.bucket or sops_extract("kopia_b2_bucket")
    stats = {}
    lines = b2_longhorn_lines(
        sops_extract("kopia_b2_key_id"),
        sops_extract("kopia_b2_application_key"),
        bucket,
        ns.prefix,
        _stats=stats,
    )
    record_b2_spend(
        "b2-longhorn",
        class_c=stats.get("class_c", 0),
        note=f"{stats.get('pages', 0)} pages",
    )
    text, code = format_longhorn_summary(parse_longhorn_listing(lines))
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
