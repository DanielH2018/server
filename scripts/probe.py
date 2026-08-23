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
from zoneinfo import ZoneInfo

# `core.<name>` for anything the tests monkeypatch — fetch, k8s_namespace,
# metallb_vip, sops_extract. Binding those into this module's globals with a
# `from probe_core import ...` would take a snapshot the patch never reaches.
# Everything else is imported by name, since nothing patches it.
import probe_core as core
from probe_core import (
    PI_HOST,
    SECRETS_PATH,
    _rows_from_loki,
    config_get,
    curl_argv,
    ha_curl_argv,
    k8s_endpoint,
    loki_endpoint,
    loki_labels_url,
    loki_query_url,
    pi_resolve,
    pi_url,
    prom_endpoint,
    prom_query_url,
    prom_targets_url,
    scrutiny_url,
    since_window_ns,
)
from probe_ha import run_ha, run_ha_state
from probe_storage import (
    LONGHORN_PREFIX,
    run_b2_budget,
    run_b2_longhorn,
    run_b2_record,
    run_b2_spend,
)


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


# kuma-drift: declared monitors vs live ones
#
# `monitors` divides the exporter's monitor count by itself, so it reports "N/N up" over
# whatever Kuma happens to be exporting. A monitor that is declared and never created, or
# created and then paused, is absent from that set and therefore absent from the ratio — it
# reads as green. `test_kuma_static_monitors.py` has the same blind spot from the other end:
# it validates the declaration file against itself and never asks what is live.
#
# The instance that motivated this: `WG Pi Peer Backup` lost its heartbeat to a NetworkPolicy
# on 2026-08-20, and `monitors` reported 81/81 up for a day with the tile simply gone.
#
# Declared names are read straight out of the template rather than rendered through Jinja: every
# `"name"` in it is a literal, and parsing beats standing up a Jinja environment with a stub for
# every push token just to recover strings that were never templated.
STATIC_MONITORS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible",
    "roles",
    "k8s",
    "uptime-kuma",
    "templates",
    "static-monitors.yaml.j2",
)

# Seconds of grace on top of a monitor's own interval before its absence counts as drift: Kuma's
# exporter and Prometheus's scrape each sit between a heartbeat landing and this query seeing it.
KUMA_EXPORT_SLACK = 120

_ENTITY_NAME_RE = re.compile(r'"name":\s*"([^"]+)"')
_ENTITY_TYPE_RE = re.compile(r'"type":\s*"([a-z]+)"')
_ENTITY_INTERVAL_RE = re.compile(r'"interval":\s*(\d+)')
_JINJA_IF_RE = re.compile(r"{%-?\s*if\b")
_JINJA_ENDIF_RE = re.compile(r"{%-?\s*endif\b")
# The condition itself, so a gated monitor can be checked against the variable rather than
# excused on the strength of the `{% if %}` existing. First identifier wins: every gate in
# this template is `{% if <secret_name> | default('') %}`.
_JINJA_IF_COND_RE = re.compile(r"{%-?\s*if\s+([a-zA-Z_][a-zA-Z0-9_]*)")


def parse_declared_monitors(text):
    """Monitor declarations from the static-monitors template.

    Returns {name: {"type": str, "interval": int|None, "gated": bool, "gate": str|None}}.
    `gated` marks an entity inside a `{% if <token> %}` block and `gate` names the variable it
    is gated on, innermost first.

    `gate` exists because `gated` alone was a licence to ignore. Until 2026-08-22 a gated
    monitor's absence was excused unconditionally, on the reasoning that it "renders away when
    the secret is unset" — which is an assumption about the secret, not a reading of it.
    Measured that day: `etcd_snapshot_push_token` was set (32 chars, in the rotation registry
    since 2026-07-04), the Off-box etcd Snapshot monitor was NOT live, and this check reported
    it as correctly skipped. A gated monitor that vanishes is invisible twice over — absent
    from the exporter, and excused by the drift check written to catch exactly that. Naming
    the variable lets the caller resolve it and tell the two cases apart.
    """
    declared, gates = {}, []
    for line in text.splitlines():
        for cond in _JINJA_IF_COND_RE.findall(line):
            gates.append(cond)
        # A bare `{% if %}` with no leading identifier still opens a scope; keep the stack
        # aligned with the nesting rather than with the conditions we could parse.
        gates.extend(
            [None]
            * (len(_JINJA_IF_RE.findall(line)) - len(_JINJA_IF_COND_RE.findall(line)))
        )
        for _ in range(len(_JINJA_ENDIF_RE.findall(line))):
            if gates:
                gates.pop()
        name = _ENTITY_NAME_RE.search(line)
        kind = _ENTITY_TYPE_RE.search(line)
        if not name or not kind:
            continue
        if kind.group(1) == "notification":  # not a monitor; never in monitor_status
            continue
        interval = _ENTITY_INTERVAL_RE.search(line)
        innermost = next((g for g in reversed(gates) if g), None)
        declared[name.group(1)] = {
            "type": kind.group(1),
            "interval": int(interval.group(1)) if interval else None,
            "gated": bool(gates),
            "gate": innermost,
        }
    return declared


def gate_var_state(var):
    """True / False / None for whether a gating secret has a non-empty value.

    None means "could not be read" — no age key on this host, sops missing, key absent — and
    is deliberately distinct from False. Reporting an unreadable gate as unset is what made
    the old check silent; an unreadable input and an empty one must not look alike.
    """
    out = subprocess.run(
        ["sops", "-d", "--extract", f'["{var}"]', SECRETS_PATH],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def format_kuma_drift(declared, live, kuma_age_seconds, gate_states=None):
    """Compare declared monitor names against the live exporter's. Pure.

    `live` is the set of monitor_name labels; `kuma_age_seconds` is how long the Kuma pod has
    been up, or None when that could not be read.

    Kuma's exporter emits a monitor only once it has received a heartbeat since the process
    started, so a restart empties the series for EVERY monitor — http and port tiles included,
    not just push ones — and each returns on its next beat. A monitor whose interval has not
    elapsed since the restart is therefore PENDING, not missing; reporting it as drift would
    make this check cry wolf after every deploy. Measured on the 2026-08-21 rollout: 24 of 82
    monitors were exported 88 seconds in, and the absent ones spanned every type.

    KUMA_EXPORT_SLACK covers the two lags between a beat and this query seeing it: Kuma's own
    scrape endpoint and Prometheus's scrape interval. Without it a 60s http monitor reads as
    missing at 88s of uptime, which is what the first run of this check did.

    An unreadable pod age is treated as a long uptime: it fails loud rather than quiet, matching
    `health`'s unreadable-restart-time rule.
    """
    gate_states = gate_states or {}
    missing, pending, gated, unverified = [], [], [], []
    for name, spec in sorted(declared.items()):
        if name in live:
            continue
        state = gate_states.get(spec.get("gate")) if spec["gated"] else False
        if spec["gated"] and state is None:
            unverified.append(
                f"  {name}: gated on {spec['gate']}, which could not be read"
            )
        elif spec["gated"] and state is False:
            gated.append(name)
        elif (
            kuma_age_seconds is not None
            and spec["interval"] is not None
            and kuma_age_seconds < spec["interval"] + KUMA_EXPORT_SLACK
        ):
            pending.append(f"  {name}: no beat due yet ({spec['interval']}s interval)")
        else:
            missing.append(f"  {name}: declared, not live")
    orphans = [f"  {n}: live, not declared" for n in sorted(live - set(declared))]

    lines = [f"{len(live)} live / {len(declared)} declared"]
    if kuma_age_seconds is not None and pending:
        lines.append(
            f"  (kuma up {int(kuma_age_seconds)}s — monitors below not yet due)"
        )
    lines.extend(missing + orphans + pending + unverified)
    if gated:
        lines.append(
            f"  {len(gated)} gated on a secret that is genuinely unset, skipped: "
            f"{', '.join(gated)}"
        )
    if missing or orphans:
        return "\n".join(lines), 1
    return "\n".join(lines), 0


def run_kuma_drift(ns):
    """Reconcile the declared monitor set against the live one (exit 0 = no drift)."""
    base, pin = prom_endpoint()
    url = prom_query_url(base, 'monitor_status{job="uptime-kuma"}')
    if ns.dry_run:
        print(" ".join(curl_argv(url, resolve=pin)))
        return 0
    with open(STATIC_MONITORS_PATH) as f:
        declared = parse_declared_monitors(f.read())
    try:
        data = json.loads(core.fetch(url, resolve=pin))
    except json.JSONDecodeError:
        print("prometheus returned non-JSON (query endpoint down?)")
        return 1
    live = {
        (s.get("metric") or {}).get("monitor_name")
        for s in data.get("data", {}).get("result", [])
    }
    live.discard(None)
    # Resolved only for gates whose monitor is actually absent — a sops call per gate is the
    # cost, and a monitor that is live needs no explanation for why it might not be.
    #
    # DECIDED: the decrypt stays ON by default, and `--no-secrets` is the opt-out — not the
    # reverse. This subcommand is allow-listed and so runs unprompted, which is a fair reason to
    # want no SOPS read on the path; but assuming a gate was unset is exactly the miss 16cf5721
    # fixed on 2026-08-22, and defaulting to --no-secrets would reinstate it. The read is
    # narrow: `var` comes from _JINJA_IF_COND_RE, constrained to [a-zA-Z_][a-zA-Z0-9_]*, and is
    # passed as an argv element rather than through a shell, so no value and no injection point
    # escapes gate_var_state — only bool(stdout) does. Reach for --no-secrets when the age key
    # should not be touched at all; accept "unverified" as the cost.
    gate_states = {
        spec["gate"]: None if ns.no_secrets else gate_var_state(spec["gate"])
        for name, spec in declared.items()
        if spec["gate"] and name not in live
    }
    text, code = format_kuma_drift(
        declared, live, kuma_pod_age_seconds(), gate_states=gate_states
    )
    print(text)
    if ns.no_secrets and gate_states:
        # Without this the gates read "could not be read", which is the wording for a genuine
        # failure — no age key, sops missing. Deliberately not reading and failing to read must
        # not look alike; that conflation is the recurring shape this estate keeps paying for.
        print("  (gates unverified by request: --no-secrets, no SOPS read attempted)")
    return code


def kuma_pod_age_seconds():
    """Seconds since the uptime-kuma pod started, or None if that cannot be read."""
    out = subprocess.run(
        k8s_pods_argv("uptime-kuma", core.k8s_namespace()),
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    try:
        pods = json.loads(out.stdout).get("items", [])
    except json.JSONDecodeError:
        return None
    starts = [
        _seconds_since(
            (p.get("status") or {}).get("startTime"), datetime.now(timezone.utc)
        )
        for p in pods
    ]
    starts = [s for s in starts if s is not None]
    return min(starts) if starts else None


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


# alert history (pure)
# monitor-bridge is the homelab's alert brain: every INTERVAL it pushes each check's
# state to a Kuma push monitor and logs "[<ts>] DOWN <name> - <msg> (<n> cycles)" for
# any check that's firing. Kuma keeps only current state; Loki keeps the log lines
# (31d retention), so the history of *what alerted, when* is these DOWN lines. This
# collapses the every-cycle repeats into one row per firing episode.
#
# It is NOT the only alert path, which is why this command reads two streams. Several host
# crons push their own Kuma monitors directly and never pass through monitor-bridge, so
# monitor-bridge's container log contains nothing about them — it polls no Kuma state. Reading
# only that stream made the backup plane's sole DOWN signal invisible: measured 2026-08-22,
# 465 `longhorn-backup-health: status=down` lines over 7 days appeared in no episode list,
# while `monitor_status{monitor_name="Manifest Prune Drift"}` read 0 and `alerts --check
# manifest` printed "no DOWN alerts". See SYSLOG_ALERT_LOGQL below for the second stream.
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


# The second alert path: host crons that push Kuma directly and log through `logger`. rsyslog
# prefixes every one of those lines, so the shape is NOT the bare "<tag>: status=down <msg>" a
# reading of the cron scripts suggests — it is
#   "<iso-ts> <host> <tag>: status=down <msg>"
# and, when the push itself fails (the case where syslog is the ONLY record, since Kuma never
# learned),
#   "<iso-ts> <host> <tag>: push failed (status=down: <msg>)"
# Measured over 7 days on 2026-08-22, `|= "status=down"` matched exactly three tags —
# longhorn-backup-health (465 lines), manifest-prune-check (2) and claude-otel-health (1) — so
# the filter is precise, not a net that drags in unrelated syslog traffic.
#
# COVERAGE IS PARTIAL AND DELIBERATE. Two pushers emit no `status=` token at all and stay
# invisible here: secret-rotation-audit logs a bare reason string, and live_drift_check's cron
# pipes nothing to `logger`. Both fixes are one-line edits to files this change does not own
# (roles/k8s/.../secret-rotation-audit.sh.j2 and setup/k3s/tasks/health-crons.yml). Confirmed
# absent, not merely unmatched: a 7-day Loki query for either name returned "no logs".
SYSLOG_ALERT_LOGQL = '{job="syslog"} |= "status=down"'
_SYSLOG_LINE_RE = re.compile(
    r"^\S+\s+\S+\s+(?P<name>[A-Za-z0-9_.-]+?)(?:\[\d+\])?:\s+(?P<rest>.*status=down.*)$"
)
# The closing paren is OPTIONAL because rsyslog truncates a long line — observed on
# longhorn-backup-health, whose status message runs past the limit and arrives with no closing
# paren at all. Anchoring on `\)$` dropped those lines to the raw fallback below, printing the
# "push failed (status=down: " scaffolding as if it were the message.
_SYSLOG_PUSH_FAILED_RE = re.compile(r"^push failed \(status=down:\s*(?P<msg>.*?)\)?$")
_SYSLOG_STATUS_RE = re.compile(r"^status=down\s*(?P<msg>.*)$")


def parse_syslog_down_line(line):
    """(cron_tag, msg) for a host cron's syslog DOWN line, else None.

    The tag is the episode name, matching what `--check` filters on — `manifest-prune-check`,
    `longhorn-backup-health`. Those are machine names like monitor-bridge's own check names,
    not Kuma display names, so one `--check` substring keeps working across both streams.

    A failed push keeps its "push failed:" prefix in the message. That is the operator's cue
    that Kuma never learned about this DOWN, so syslog is the only place it is recorded.
    """
    m = _SYSLOG_LINE_RE.match(line)
    if not m:
        return None
    rest = m["rest"]
    hit = _SYSLOG_STATUS_RE.match(rest)
    if hit:
        return m["name"], hit["msg"].strip()
    hit = _SYSLOG_PUSH_FAILED_RE.match(rest)
    if hit:
        return m["name"], f"push failed: {hit['msg'].strip()}"
    return m["name"], rest.strip()


# Each alert stream with the parser that reads its line shape. run_alerts queries both and
# merges the rows: LogQL cannot OR two stream selectors that share no label name, and
# `container` (monitor-bridge) and `job` (syslog) share none.
ALERT_SOURCES = (
    (ALERT_LOGQL, parse_down_line),
    (SYSLOG_ALERT_LOGQL, parse_syslog_down_line),
)


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
        f"{len(episodes)} DOWN episode(s), last {days:g}d "
        "(monitor-bridge + host crons -> Kuma):"
    )
    lines = [header, ""]
    for e in episodes:
        dur = _fmt_duration(e["last_ns"] - e["first_ns"])
        lines.append(
            f"{_fmt_local(e['first_ns'])}  {dur:>6}  "
            f"{e['name']:<{width}}  {e['cycles']:>3}c  {e['msg'][:88]}"
        )
    return "\n".join(lines)


# routing (pure given resolve_ip)


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
    kd = sub.add_parser(
        "kuma-drift",
        help="declared monitors vs live ones — catches a tile that is gone rather "
        "than down, which `monitors` counts as green (exit 0 = no drift)",
    )
    kd.add_argument(
        "--no-secrets",
        action="store_true",
        help="skip the SOPS reads that resolve why a gated monitor is absent; those gates "
        "report as unverified instead of gated/ungated",
    )
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
        start, end = since_window_ns(getattr(ns, "since", None))
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


# runtime (impure)


def resolve_ip(container):
    out = subprocess.run(inspect_ip_argv(container), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"docker inspect {container} failed: {out.stderr.strip()}")
    ip = parse_ip(out.stdout)
    if not ip:
        raise SystemExit(f"{container} has no container IP (is it running?)")
    return ip


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
    ns = core.k8s_namespace()
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


def run_query(ns):
    """Fetch a metric / loki-query and print the formatted view (the default).
    `--json` and `--dry-run` never reach here — they take the raw streaming path."""
    if ns.cmd == "metric":
        base, pin = prom_endpoint()
        url = prom_query_url(base, ns.promql)
        formatter = format_metric
    else:
        base, pin = loki_endpoint()
        # `metric` shares this function and its subparser declares no --since, so read the
        # attribute defensively. No `direction`: Loki's default `backward` is what makes
        # --limit return the NEWEST N lines, which format_loki then sorts oldest-first.
        # run_alerts' `direction=forward` is for episode reconstruction and does not belong here.
        start, end = since_window_ns(getattr(ns, "since", None))
        url = loki_query_url(base, ns.logql, ns.limit, start=start, end=end)
        formatter = format_loki
    body = core.fetch(url, resolve=pin)
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
    body = core.fetch(url, resolve=pin)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(body.strip())
        return 1
    text, code = format_monitor_status(data)
    print(text)
    return code


def alert_source_urls(base, days, limit):
    """The Loki URLs `alerts` fetches, one per stream in ALERT_SOURCES.

    `direction=forward` because episode reconstruction walks samples oldest-first; that is this
    command's need, not loki-query's — see run_query.
    """
    end_s = datetime.now(_CHICAGO).timestamp()
    start_s = end_s - days * 86400
    return [
        loki_query_url(
            base,
            logql,
            limit,
            start=int(start_s * 1e9),
            end=int(end_s * 1e9),
            direction="forward",
        )
        for logql, _ in ALERT_SOURCES
    ]


def run_alerts(ns):
    """Fetch DOWN log lines from every alert stream over the window and print firing episodes.

    Both streams are queried and their rows merged before episodes are built, so one episode
    list covers monitor-bridge's checks and the host crons that push Kuma directly. `--check`
    filters both, because both name episodes with a machine name rather than a Kuma display
    name.
    """
    base, pin = loki_endpoint()
    urls = alert_source_urls(base, ns.days, ns.limit)
    if ns.dry_run:
        for url in urls:
            print(" ".join(curl_argv(url, resolve=pin)))
        return 0
    raw, rows, truncated = [], [], []
    for url, (logql, parser) in zip(urls, ALERT_SOURCES):
        fetched = _rows_from_loki(json.loads(core.fetch(url, resolve=pin)))
        # Per stream, not on the merged list: one stream hitting the cap says nothing about
        # the other, and reporting the union would cry truncation whenever the totals summed
        # past the limit.
        if len(fetched) >= ns.limit:
            truncated.append(logql)
        raw.extend(fetched)
        for ns_ts, line in fetched:
            parsed = parser(line)
            if parsed is None:
                continue
            name, msg = parsed
            if ns.check and ns.check.lower() not in name.lower():
                continue
            rows.append((ns_ts, name, msg))
    raw.sort()
    if ns.raw:
        print("\n".join(line for _, line in raw) or "no logs")
    else:
        episodes = alert_episodes(rows, ns.gap_min * 60)
        if ns.json:
            print(json.dumps(episodes, indent=2))
        else:
            print(format_alert_episodes(episodes, ns.days))
    for logql in truncated:
        print(
            f"\n(warning: hit --limit {ns.limit} log lines on {logql} — results may be "
            "truncated; raise --limit or narrow --days)"
        )
    return 0


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

    ns = core.k8s_namespace()
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
    body = config_get(url, arr_curl_config(core.sops_extract(f"{ns.app}_api_key")))
    if ns.json:
        print(body, end="")
        return 0
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body.strip())
        return 1
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
                ns_name = core.k8s_namespace()
                print(" ".join(k8s_deploy_argv(ns.container, ns_name)))
                print(
                    " ".join(k8s_deploy_argv(ns.container, ns_name, kind="daemonset"))
                    + "   # only if the Deployment lookup misses"
                )
                print(" ".join(k8s_pods_argv(ns.container, ns_name)))
            return 0
        return run_health(ns.container, docker=ns.docker)
    # Subcommands that answer from an API rather than streaming a shell pipeline. Each one is
    # `run_X(ns) -> int`, so the table is the whole dispatch — adding a subcommand is a parser
    # entry plus a row here. Built inside main() deliberately: run_b2_longhorn and its B2/Longhorn
    # siblings are defined BELOW main(), so a module-level table would name them before they exist.
    handlers = {
        # `ha` resolves a token + talks to the HA REST API.
        "ha": run_ha,
        "arr": run_arr,
        "alerts": run_alerts,
        "b2-longhorn": run_b2_longhorn,
        "b2-budget": run_b2_budget,
        "b2-spend": run_b2_spend,
        "b2-record": run_b2_record,
        "ha-state": run_ha_state,
        "monitors": run_monitors,
        "kuma-drift": run_kuma_drift,
    }
    if ns.cmd in handlers:
        return handlers[ns.cmd](ns)
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
