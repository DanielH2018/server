"""Home Assistant: live state, automations, and the read-only WebSocket trace client.

Backs the `ha` and `ha-state` subcommands. The alias-slug-vs-id trap lives here
(`match_automation`), as does the minimal stdlib WebSocket client used only for
reading automation traces.
"""

import glob
import json
import os
import re
import socket
import ssl

import probe_core as core
from probe_core import (
    DEFAULT_TIMEOUT,
    config_get,
    ha_base,
    ha_curl_argv,
    ha_host,
    ha_resolve,
)

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

# Git-managed automation source (repo-root relative to this file) — the "expected" set for
# the verify-automations post-deploy gate. The deployed config is copied from here verbatim,
# one file per topic, merged by `!include_dir_merge_list` in configuration.yaml.
# `k8s`, not `containers`: HA moved at the slice-5 B3 cutover and this constant did not follow,
# so the gate raised FileNotFoundError from the cutover until the 2026-08-16 review. The old
# test only asserted argparse wiring and never opened the file — test_verify_automations_path_exists
# now pins the path itself.
AUTOMATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ansible",
    "roles",
    "k8s",
    "home-assistant",
    "files",
    "automations",
)


def automations_source_text(directory: str = AUTOMATIONS_DIR) -> str:
    """Every *.yaml under the automations directory, concatenated.

    An empty directory is an error rather than an empty expected set: a gate that expects nothing
    passes on anything.
    """
    paths = sorted(glob.glob(os.path.join(directory, "*.yaml")))
    if not paths:
        raise FileNotFoundError(f"no *.yaml under {directory}")
    parts = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n".join(parts)


# Top-level automation list items only: `- id: <slug>` anchored at column 0. A trigger/condition
# `id:` is always indented, so it can never be mistaken for an automation id.
_AUTOMATION_ID_RE = re.compile(r"^- id:\s*(\S+)", re.MULTILINE)

# The generated snapshot of integration-provided entities that validate_ha_config.py resolves
# config references against — the "expected" set for the verify-entities gate.
EXTERNAL_ENTITIES_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
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


# Home Assistant (pure)


def ha_state_url(base, entity_id):
    return f"{base}/api/states/{entity_id}"


def ha_get_url(base, path):
    """URL for an arbitrary HA REST path under `base` (scheme://host, no trailing slash).

    Normalizes a leading `/` and an `api/` prefix so `error_log`, `/error_log`, and `/api/error_log`
    all work.
    """
    path = path.lstrip("/")
    if path.startswith("api/"):
        path = path[len("api/") :]
    return f"{base}/api/{path}"


def ha_curl_config(token):
    """The `curl --config -` body carrying the auth header (consumed via stdin)."""
    return f'header = "Authorization: Bearer {token}"\n'


# Minimal synchronous WebSocket client (stdlib only — no `websockets` dep)
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
        """Read and return exactly `n` bytes from `sock`, buffering across recv() calls."""
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
    """Human timeline from a trace/get result:

    trigger -> each step path (+ PASS/FAIL for a condition step, whose result is {"result": bool})
    -> error.

    HA's trace/get payload has `trigger` as a plain string description (e.g. "state of
    binary_sensor.aqara_fp300_presence"); older/nested shapes may be a dict with a `description` key
    — both are handled.
    """
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
    """The `id:` of every top-level automation in the automations/ source text.

    Regex over the raw text (no YAML parse) — robust to the HA Jinja inside the file; ids are simple
    slugs.
    """
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
    """Return one error string per automation id that is defined but did not load cleanly.

    `expected_ids` are ids from files/automations/*.yaml; `live_automations` are the
    automation.* entries from /api/states. A defined id with no live automation carrying
    that attributes.id did NOT load (dropped). A defined id whose live automation is
    `unavailable` errored at load. A disabled automation (state 'off') is fine. Live ids
    not in the file (UI/.storage cruft) are ignored — this gate is file-driven so cruft
    can't make it red.
    """
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
                f"automation {aid} is defined in files/automations/ but did not load"
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
    """Fetch the latest execution trace for an automation via the HA WebSocket API.

    Read-only: sends ONLY auth + trace/list + trace/get. Returns the trace dict, or None if no
    stored trace.

    `host` is the unsuffixed .local hostname (TLS on 443, SNI/Host). `connect_ip` pins the TCP
    connection to the ingress VIP — since the bridge teardown (slice-7 BT4) the host shell's DNS
    answer for the name is not the cluster edge (see ha_host).
    """
    import base64
    import os

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
    """Find an automation in a `/api/states` list by entity_id, `attributes.id`, or slug.

    Resolves the alias-slug-vs-id trap: an automation's entity_id derives from its
    *alias*, not its `id`, so the two can differ (e.g. id `bedroom_fan_temperature` ->
    `automation.bedroom_fan_temperature_control`). Accepts a bare slug/id or a full
    `automation.<slug>` entity_id. Returns None if no match.
    """
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
    for _name, cell in model["cells"].items():
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


# low-level argv / parsing helpers (pure)


def ha_token():
    """Decrypt claude_ha_token from the SOPS secrets file.

    Requires the host's age key (present on daniel-server, where HA runs).
    """
    return core.sops_extract("claude_ha_token")


def ha_get(url, token, resolve=None):
    """Authenticated HA GET; returns the response body. Token is passed via stdin."""
    return config_get(url, ha_curl_config(token), resolve=resolve)


def _ha_url(ip, ns):
    if ns.ha_cmd == "state":
        return ha_state_url(ip, ns.entity_id)
    if ns.ha_cmd == "automation":
        return ha_get_url(ip, "states")  # fetch all, then match locally
    return ha_get_url(ip, ns.path)  # get


def run_ha(ns):
    """Dispatch a parsed `ha` subcommand against the live HA API.

    Handles trace/why, verify-automations, verify-entities, get, state, and a bare
    automation lookup.

    Args:
        ns: The parsed argparse namespace for the `ha` subcommand.

    Returns:
        0 on success, 1 when the check itself found a problem (e.g. an automation not
        found, a vanished entity, an unparseable body).
    """
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
                ha_trace(ha_host(), token, automation_id, connect_ip=core.metallb_vip())
            )
        )
        return 0
    if ns.ha_cmd == "verify-automations":
        if ns.dry_run:
            print(
                " ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states")))
                + f"   # + Bearer; compare attributes.id against ids in {AUTOMATIONS_DIR}/*.yaml"
            )
            return 0
        states = json.loads(
            ha_get(ha_get_url(ha_base(), "states"), ha_token(), resolve=ha_resolve())
        )
        live = [s for s in states if s.get("entity_id", "").startswith("automation.")]
        expected = expected_automation_ids(automations_source_text())
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
    """Print the derived state-model rows, and the full entity inventory if requested.

    Args:
        ns: The parsed argparse namespace for the `ha state` subcommand.
    """
    import json
    from home_assistant import ha_state_model

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
