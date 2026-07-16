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
    loki-labels              Loki label names                (loki :3100)
    loki-query '<logql>'     Loki range query [--limit N] [--json] (loki :3100)
    scrutiny                 Disk SMART summary              (scrutiny :8080)
    pi <subpath>             Pi glances API, e.g. `pi fs`    (daniel-pi.lan:61208)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]
    health <container>       Container state + healthcheck rollup (exit 0 = healthy)
    arr <app> <api-path>     Read-only *arr API GET [--json] (sonarr/radarr/prowlarr)
    ha state <entity_id>     Live HA entity state + attrs    (home-assistant :8123)
    ha automation <id|alias> One automation's on/off + last_triggered (resolves alias!=id)
    ha get <api-path>        Raw GET /api/<path>, e.g. `ha get error_log`
    ha trace <id|alias>      Why an automation last ran/no-op'd (per-condition WS trace; alias: why)
    ha verify-automations    Assert every automation in automations.yaml loaded (exit 0 = all loaded)
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
from urllib.parse import urlencode

DEFAULT_TIMEOUT = 10
HA_PORT = 8123
HA_CONTAINER = "home-assistant"
# claude_ha_token lives in the SOPS-encrypted secrets file (repo-root relative).
SECRETS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "vars",
    "secrets.yml",
)

# Git-managed automation source (repo-root relative to this file) — the "expected" set for
# the verify-automations post-deploy gate. The deployed config is copied from here verbatim.
AUTOMATIONS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "roles",
    "containers",
    "home-assistant",
    "files",
    "automations.yaml",
)

# Top-level automation list items only: `- id: <slug>` anchored at column 0. A trigger/condition
# `id:` is always indented, so it can never be mistaken for an automation id.
_AUTOMATION_ID_RE = re.compile(r"^- id:\s*(\S+)", re.MULTILINE)

# --- URL builders (pure) ----------------------------------------------------


def prom_query_url(ip, promql):
    return f"http://{ip}:9090/api/v1/query?" + urlencode({"query": promql})


def prom_targets_url(ip):
    return f"http://{ip}:9090/api/v1/targets"


def loki_labels_url(ip):
    return f"http://{ip}:3100/loki/api/v1/labels"


def loki_query_url(ip, logql, limit):
    return f"http://{ip}:3100/loki/api/v1/query_range?" + urlencode(
        {"query": logql, "limit": limit}
    )


def scrutiny_url(ip):
    return f"http://{ip}:8080/api/summary"


def pi_url(subpath):
    return f"http://daniel-pi.lan:61208/api/4/{subpath}"


# --- Home Assistant (pure) --------------------------------------------------


def ha_state_url(ip, entity_id):
    return f"http://{ip}:{HA_PORT}/api/states/{entity_id}"


def ha_get_url(ip, path):
    """URL for an arbitrary HA REST path. Normalizes a leading `/` and an
    `api/` prefix so `error_log`, `/error_log`, and `/api/error_log` all work."""
    path = path.lstrip("/")
    if path.startswith("api/"):
        path = path[len("api/") :]
    return f"http://{ip}:{HA_PORT}/api/{path}"


def ha_curl_argv(url, timeout=DEFAULT_TIMEOUT):
    """curl argv for an HA GET. The bearer header is fed via stdin (`--config -`,
    see ha_curl_config), so the token NEVER appears in argv / `ps` / shell history."""
    return ["curl", "-sS", "--max-time", str(timeout), "--config", "-", url]


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


def ha_trace(ip, token, automation_id, timeout=DEFAULT_TIMEOUT):
    """Fetch the latest execution trace for an automation via the HA WebSocket API. Read-only:
    sends ONLY auth + trace/list + trace/get. Returns the trace dict, or None if no stored trace."""
    import base64
    import os
    import socket

    sock = socket.create_connection((ip, HA_PORT), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET /api/websocket HTTP/1.1\r\nHost: {ip}:{HA_PORT}\r\n"
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


def curl_argv(url, timeout=DEFAULT_TIMEOUT):
    return ["curl", "-sS", "--max-time", str(timeout), url]


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
    sub.add_parser("scrutiny", help="disk SMART summary")
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
        "health", help="container state + healthcheck rollup (exit 0 = healthy)"
    )
    hl.add_argument("container", help="container name, e.g. jellyfin")
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
    hst = sub.add_parser("ha-state", help="live view of the derived state model")
    hst.add_argument(
        "--inventory",
        action="store_true",
        help="also dump every live entity grouped by domain",
    )
    return p


def plan(args, resolve_ip):
    """Return the command pipeline (list of argv stages) for the parsed args.

    `resolve_ip(container) -> ip` is injected so all routing/URL logic is testable
    without Docker or the network. Most commands are a single stage; `cert` is a
    two-stage openssl pipeline.
    """
    ns = _build_parser().parse_args(args)
    cmd = ns.cmd
    if cmd == "metric":
        return [curl_argv(prom_query_url(resolve_ip("prometheus"), ns.promql))]
    if cmd == "targets":
        return [curl_argv(prom_targets_url(resolve_ip("prometheus")))]
    if cmd == "loki-labels":
        return [curl_argv(loki_labels_url(resolve_ip("loki")))]
    if cmd == "loki-query":
        return [curl_argv(loki_query_url(resolve_ip("loki"), ns.logql, ns.limit))]
    if cmd == "scrutiny":
        return [curl_argv(scrutiny_url(resolve_ip("scrutiny")))]
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


def fetch(url):
    """Run the read-only curl GET and return its body (raise on failure)."""
    out = subprocess.run(curl_argv(url), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"curl {url} failed: {out.stderr.strip()}")
    return out.stdout


def run_query(ns):
    """Fetch a metric / loki-query and print the formatted view (the default).
    `--json` and `--dry-run` never reach here — they take the raw streaming path."""
    if ns.cmd == "metric":
        url = prom_query_url(resolve_ip("prometheus"), ns.promql)
        formatter = format_metric
    else:
        url = loki_query_url(resolve_ip("loki"), ns.logql, ns.limit)
        formatter = format_loki
    body = fetch(url)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(body.strip())
        return 1
    print(formatter(data))
    return 0


def run_health(container):
    out = subprocess.run(inspect_argv(container), capture_output=True, text=True)
    try:
        data = json.loads(out.stdout) if out.returncode == 0 else []
    except json.JSONDecodeError:
        data = []
    text, code = format_health(data, container)
    print(text)
    return code


def ha_token():
    """Decrypt claude_ha_token from the SOPS secrets file. Requires the host's age
    key (present on daniel-server, where HA runs)."""
    return sops_extract("claude_ha_token")


def ha_get(url, token):
    """Authenticated HA GET; returns the response body. Token is passed via stdin."""
    return config_get(url, ha_curl_config(token))


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


def config_get(url, config_body):
    """Authenticated GET whose auth header is fed via curl `--config -` stdin
    (never argv). Returns the response body."""
    out = subprocess.run(
        ha_curl_argv(url), input=config_body, capture_output=True, text=True
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
                f"ws://<ha-ip>:{HA_PORT}/api/websocket  trace/list+trace/get for {ns.query!r} "
                f"# + auth Bearer <redacted>"
            )
            return 0
        ip = resolve_ip(HA_CONTAINER)
        token = ha_token()
        states = json.loads(ha_get(ha_get_url(ip, "states"), token))
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
        print(format_trace(ha_trace(ip, token, automation_id)))
        return 0
    if ns.ha_cmd == "verify-automations":
        if ns.dry_run:
            print(
                " ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states")))
                + f"   # + Bearer; compare attributes.id against ids in {AUTOMATIONS_YAML}"
            )
            return 0
        ip = resolve_ip(HA_CONTAINER)
        states = json.loads(ha_get(ha_get_url(ip, "states"), ha_token()))
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
    if ns.dry_run:
        argv = ha_curl_argv(_ha_url("<ha-ip>", ns))
        print(
            " ".join(argv)
            + "   # + Authorization: Bearer <redacted> (via --config stdin)"
        )
        return 0
    body = ha_get(_ha_url(resolve_ip(HA_CONTAINER), ns), ha_token())
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
    body = ha_get(ha_get_url(resolve_ip(HA_CONTAINER), "states"), ha_token())
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
            print(" ".join(inspect_argv(ns.container)))
            return 0
        return run_health(ns.container)
    # `ha` resolves a token + talks to the HA REST API rather than streaming a pipeline.
    if ns.cmd == "ha":
        return run_ha(ns)
    if ns.cmd == "arr":
        return run_arr(ns)
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


if __name__ == "__main__":
    sys.exit(main())
