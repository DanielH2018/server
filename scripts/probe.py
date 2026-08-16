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


def scrutiny_url(base):
    return f"{base}/api/summary"


PI_HOST = "daniel-pi"


def pi_url(subpath):
    return f"http://daniel-pi.lan:61208/api/4/{subpath}"


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
    sub.add_parser("loki-labels", help="Loki label names")
    lq = sub.add_parser("loki-query", help="Loki range query")
    lq.add_argument("logql")
    lq.add_argument("--limit", type=int, default=100)
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
    b2l.add_argument("--bucket", help="override the bucket from kopia's repo config")
    b2l.add_argument(
        "--prefix", default=LONGHORN_PREFIX, help="B2 prefix Longhorn writes under"
    )
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


def plan(args, resolve_ip, k8s_endpoint=k8s_endpoint):
    """Return the command pipeline (list of argv stages) for the parsed args.

    `resolve_ip(container) -> ip` and `k8s_endpoint(hostname) -> (base, pin)` are injected so
    all routing/URL logic is testable without Docker, SOPS, or the network. Most
    commands are a single stage; `cert` is a two-stage openssl pipeline.
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
        return [curl_argv(loki_query_url(base, ns.logql, ns.limit), resolve=pin)]
    if cmd == "scrutiny":
        base, pin = k8s_endpoint("scrutiny")
        return [curl_argv(scrutiny_url(base), resolve=pin)]
    if cmd == "pi":
        return [curl_argv(pi_url(ns.subpath))]
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
    """Read-only *arr API GET. Pulls <app>_api_key from SOPS and passes it via
    stdin. Pretty-prints JSON by default; `--json` prints the raw response."""
    if ns.dry_run:
        print(
            " ".join(ha_curl_argv(arr_url("<arr-ip>", ns.app, ns.path)))
            + "   # + X-Api-Key: <redacted> (via --config stdin)"
        )
        return 0
    url = arr_url(resolve_ip(ns.app), ns.app, ns.path)
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
    if ns.cmd == "ha-state":
        return run_ha_state(ns)
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
KOPIA_REPO_CONFIG = "/app/config/repository.config"


def longhorn_lsf_argv(bucket, prefix=LONGHORN_PREFIX):
    """`rclone lsf` inside the kopia container, listing the Longhorn backup prefix.

    The B2 key is deliberately NOT in argv: `docker exec -e VAR` with no `=value` makes
    Docker inherit the value from this process's environment, so the secret never reaches
    the host process table (where `ps` would show it) nor this tool's own --dry-run output.
    """
    return [
        "docker",
        "exec",
        "-e",
        "RCLONE_CONFIG_B2_TYPE",
        "-e",
        "RCLONE_CONFIG_B2_ACCOUNT",
        "-e",
        "RCLONE_CONFIG_B2_KEY",
        "kopia",
        "rclone",
        "lsf",
        f"b2:{bucket}/{prefix}",
        "--recursive",
        "--files-only",
        "--format",
        "ps",
        "--separator",
        ";",
    ]


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


def run_b2_longhorn(ns):
    """Prove Longhorn's backups hold real data blocks in B2, not just metadata.

    The design doc's §6 gate is that a service's data must be visible in its NEW backup
    path before the Docker copy is decommissioned, so slice 2 needs this per service.

    Costs a handful of transactions (one listing, paged at 1000 objects) — negligible
    against the daily free allowance, but not free: don't put it in a loop.
    """
    # Ahead of the config read: --dry-run exists to show the command WITHOUT touching
    # anything, so it must not require a running kopia container (or any Docker at all —
    # daniel-box has none).
    if ns.dry_run:
        print(
            " ".join(
                longhorn_lsf_argv(ns.bucket or "<bucket-from-repo-config>", ns.prefix)
            )
        )
        return 0

    conf = subprocess.run(
        ["docker", "exec", "kopia", "cat", KOPIA_REPO_CONFIG],
        capture_output=True,
        text=True,
    )
    if conf.returncode != 0:
        raise SystemExit(
            "cannot read kopia's repository config: " + conf.stderr.strip()
        )
    try:
        cfg = json.loads(conf.stdout)["storage"]["config"]
    except (json.JSONDecodeError, KeyError) as e:
        raise SystemExit(f"unexpected repository config shape: {e}")

    bucket = ns.bucket or cfg.get("bucket")
    if not bucket:
        raise SystemExit(
            "no bucket in the repository config and none passed with --bucket"
        )

    argv = longhorn_lsf_argv(bucket, ns.prefix)
    env = dict(os.environ)
    env["RCLONE_CONFIG_B2_TYPE"] = "b2"
    env["RCLONE_CONFIG_B2_ACCOUNT"] = cfg.get("accessKeyID", "")
    env["RCLONE_CONFIG_B2_KEY"] = cfg.get("secretAccessKey", "")
    out = subprocess.run(argv, capture_output=True, text=True, env=env)
    if out.returncode != 0:
        # rclone echoes B2's own refusal here — including "Transaction cap exceeded".
        raise SystemExit("rclone listing failed: " + out.stderr.strip()[:400])

    text, code = format_longhorn_summary(
        parse_longhorn_listing(out.stdout.splitlines())
    )
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
