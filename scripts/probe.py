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
    metric '<promql>'        Prometheus instant query        (prometheus :9090)
    targets                  Prometheus scrape-target health (prometheus :9090)
    loki-labels              Loki label names                (loki :3100)
    loki-query '<logql>'     Loki range query [--limit N]    (loki :3100)
    scrutiny                 Disk SMART summary              (scrutiny :8080)
    pi <subpath>             Pi glances API, e.g. `pi fs`    (daniel-pi.lan:61208)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]
    health <container>       Container state + healthcheck rollup (exit 0 = healthy)
    ha state <entity_id>     Live HA entity state + attrs    (home-assistant :8123)
    ha automation <id|alias> One automation's on/off + last_triggered (resolves alias!=id)
    ha get <api-path>        Raw GET /api/<path>, e.g. `ha get error_log`

`ha` is read-only (GET) and authenticates with the SOPS-encrypted claude_ha_token
(server-only — needs the host age key). The token is fed to curl via stdin, never argv.
Add `--dry-run` to print the command(s) instead of running them.
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
    "ansible", "vars", "secrets.yml")

# --- URL builders (pure) ----------------------------------------------------


def prom_query_url(ip, promql):
    return f"http://{ip}:9090/api/v1/query?" + urlencode({"query": promql})


def prom_targets_url(ip):
    return f"http://{ip}:9090/api/v1/targets"


def loki_labels_url(ip):
    return f"http://{ip}:3100/loki/api/v1/labels"


def loki_query_url(ip, logql, limit):
    return f"http://{ip}:3100/loki/api/v1/query_range?" + urlencode({"query": logql, "limit": limit})


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
        path = path[len("api/"):]
    return f"http://{ip}:{HA_PORT}/api/{path}"


def ha_curl_argv(url, timeout=DEFAULT_TIMEOUT):
    """curl argv for an HA GET. The bearer header is fed via stdin (`--config -`,
    see ha_curl_config), so the token NEVER appears in argv / `ps` / shell history."""
    return ["curl", "-sS", "--max-time", str(timeout), "--config", "-", url]


def ha_curl_config(token):
    """The `curl --config -` body carrying the auth header (consumed via stdin)."""
    return f'header = "Authorization: Bearer {token}"\n'


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
    return (f"{obj.get('entity_id', '?')} = {obj.get('state')}  "
            f"({attrs.get('friendly_name', '?')})\n"
            f"  id={attrs.get('id')}  last_triggered={attrs.get('last_triggered')}")


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
        lines = [f"⚠ {len(anomalies)} anomaly(ies): " + "; ".join(anomalies), ""] + lines
    return "\n".join(lines)


# --- low-level argv / parsing helpers (pure) --------------------------------


def curl_argv(url, timeout=DEFAULT_TIMEOUT):
    return ["curl", "-sS", "--max-time", str(timeout), url]


def inspect_ip_argv(container):
    return ["docker", "inspect", "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container]


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
        return (f"{container}: not found (not created — wrong name, or deploy failed?)", 1)
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
    return (f"{container}: {status} (no healthcheck), restarts={restarts}",
            0 if status == "running" else 1)


def cert_stages(host, port, sni):
    """Two-stage pipeline: open a TLS session (with SNI) and decode the served
    leaf cert's subject/issuer/validity. Read-only — no data is sent."""
    s_client = ["openssl", "s_client", "-connect", f"{host}:{port}",
                "-servername", sni, "-verify_hostname", sni]
    x509 = ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates",
            "-fingerprint", "-sha256"]
    return [s_client, x509]


# --- routing (pure given resolve_ip) ----------------------------------------


def _build_parser():
    p = argparse.ArgumentParser(prog="probe.py", description="read-only homelab diagnostics")
    p.add_argument("--dry-run", action="store_true", help="print the command(s) instead of running")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metric", help="Prometheus instant query")
    m.add_argument("promql")
    sub.add_parser("targets", help="Prometheus scrape-target health")
    sub.add_parser("loki-labels", help="Loki label names")
    lq = sub.add_parser("loki-query", help="Loki range query")
    lq.add_argument("logql")
    lq.add_argument("--limit", type=int, default=100)
    sub.add_parser("scrutiny", help="disk SMART summary")
    pi = sub.add_parser("pi", help="Pi glances API")
    pi.add_argument("subpath", help="e.g. fs, quicklook, mem, cpu")
    ct = sub.add_parser("cert", help="served TLS cert details")
    ct.add_argument("target", help="host or host:port")
    ct.add_argument("--sni", help="SNI servername (defaults to host)")
    hl = sub.add_parser("health", help="container state + healthcheck rollup (exit 0 = healthy)")
    hl.add_argument("container", help="container name, e.g. jellyfin")
    ha = sub.add_parser("ha", help="Home Assistant live state (read-only, GET)")
    hasub = ha.add_subparsers(dest="ha_cmd", required=True)
    hs = hasub.add_parser("state", help="GET /api/states/<entity_id>")
    hs.add_argument("entity_id", help="e.g. fan.tower_fan")
    hs.add_argument("--json", action="store_true", help="print raw JSON")
    hauto = hasub.add_parser("automation", help="one automation by id, alias-slug, or entity_id")
    hauto.add_argument("query", help="automation id, alias-slug, or full automation.<slug>")
    hauto.add_argument("--json", action="store_true", help="print raw JSON")
    hg = hasub.add_parser("get", help="raw GET /api/<path>, e.g. error_log")
    hg.add_argument("path")
    hst = sub.add_parser("ha-state", help="live view of the derived state model")
    hst.add_argument("--inventory", action="store_true",
                     help="also dump every live entity grouped by domain")
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
            stage, stdin=prev,
            stdout=None if last else subprocess.PIPE,
            stderr=subprocess.DEVNULL if not last else None,
        )
        if prev not in (subprocess.DEVNULL, None):
            prev.close()
        prev = proc.stdout
        procs.append(proc)
    return procs[-1].wait()


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
    out = subprocess.run(
        ["sops", "-d", "--extract", '["claude_ha_token"]', SECRETS_PATH],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(
            f"could not decrypt claude_ha_token from {SECRETS_PATH}: {out.stderr.strip()}")
    return out.stdout.strip()


def ha_get(url, token):
    """Authenticated HA GET; returns the response body. Token is passed via stdin."""
    out = subprocess.run(ha_curl_argv(url), input=ha_curl_config(token),
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"curl {url} failed: {out.stderr.strip()}")
    return out.stdout


def _ha_url(ip, ns):
    if ns.ha_cmd == "state":
        return ha_state_url(ip, ns.entity_id)
    if ns.ha_cmd == "automation":
        return ha_get_url(ip, "states")  # fetch all, then match locally
    return ha_get_url(ip, ns.path)        # get


def run_ha(ns):
    if ns.dry_run:
        argv = ha_curl_argv(_ha_url("<ha-ip>", ns))
        print(" ".join(argv) + "   # + Authorization: Bearer <redacted> (via --config stdin)")
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
        print(" ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states"))) + "   # + Bearer (stdin)")
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
    if ns.cmd == "ha-state":
        return run_ha_state(ns)
    stages = plan(argv, resolve_ip)
    if ns.dry_run:
        for stage in stages:
            print(" ".join(stage))
        return 0
    return run_pipeline(stages)


if __name__ == "__main__":
    sys.exit(main())
