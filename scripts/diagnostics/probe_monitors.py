"""`probe.py monitors` and `probe.py kuma-drift` -- what is down, and what is missing.

Split out of probe.py, which had grown to 1349 lines across thirteen subcommands.

The two answer different questions. `monitors` counts the exporter's own set, so a monitor
that is GONE rather than down leaves the ratio at N/N up; `kuma-drift` diffs that set against
the declared one, which is the only way a missing monitor surfaces at all.
"""

import json
import os
import re
import subprocess

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core
from datetime import datetime, timezone

from probe_core import SECRETS_PATH, prom_endpoint, prom_query_url
from probe_health import _seconds_since, k8s_pods_argv


# Kuma's own numeric status codes, from the exporter that feeds monitor_status.
_MONITOR_STATUS_LABELS = {"0": "DOWN", "1": "UP", "2": "PENDING", "3": "MAINTENANCE"}


def format_monitor_status(data):
    """Format Kuma's monitor_status vector (Prometheus job=uptime-kuma) into a down-monitors rollup.

    Pure: takes the parsed instant-query response, returns (text, exit_code).

    Kuma keeps no history of its own — that's why `alerts` reconstructs the past from Loki instead
    of asking Kuma for it — but it does hold live state, and Prometheus already scrapes that state
    (postflight.py's check_kuma_monitors uses the same metric). No new Kuma API credential needed
    for "what's down right now" when this was already covering it.

    exit_code is 0 only when every monitor reports UP (1); PENDING and MAINTENANCE count as not-up
    too, same as DOWN, since neither means "confirmed healthy".
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
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
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
        return core.print_dry_run(url, resolve=pin)
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


def run_monitors(ns):
    """Print Kuma's live down-monitors rollup (exit 0 = all up)."""
    base, pin = prom_endpoint()
    url = prom_query_url(base, "monitor_status")
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)
    data, err = core.fetch_json(url, resolve=pin)
    if err:
        return err
    text, code = format_monitor_status(data)
    print(text)
    return code
