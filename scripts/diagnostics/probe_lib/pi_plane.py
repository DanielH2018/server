"""`probe.py targets --pi` and `probe.py pi containers` — first-command triage for daniel-pi.

`targets`, `kuma-drift`, `alerts` and `health --docker` already give the cluster a first-read
of what's wrong. daniel-pi runs no kubelet, so none of them see it: `targets` streams raw
Prometheus JSON with no Pi filter, `kuma-drift`'s declared set is the whole static-monitors
template, and `alerts` has no notion of "on the Pi" at all. This module is what `--pi` and
the `pi containers` subview dispatch to.

`kuma-drift`'s and `alerts`' `--pi` handling stay in probe_lib/monitors.py and
probe_lib/alerts.py respectively — they filter the SAME declared/live sets those subcommands
already build, so the filter belongs beside the data it filters. This module carries the two
things that have no existing home: Prometheus target scoping (no formatted view exists for
`targets` at all today) and the Pi's own Docker container inspect.
"""

import json
import re
import subprocess

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import PI_HOST, prom_endpoint, prom_targets_url

from lib.repo_paths import REPO

PROMETHEUS_TEMPLATE_PATH = (
    REPO
    / "ansible"
    / "roles"
    / "k8s"
    / "claude-otel"
    / "templates"
    / "prometheus.yaml.j2"
)

PI_ORIGIN_LABEL = "daniel-pi"

_JOB_NAME_RE = re.compile(r"^\s*-\s*job_name:\s*(\S+)")


def declared_pi_job_names(text):
    """Prometheus job names whose static target is daniel-pi's LAN IP.

    Parses claude-otel's prometheus.yaml.j2 rather than hand-listing "node-pi"/"alloy-pi": a
    job renamed or added there is picked up with no change to this file. `k8s_pi_client_ip`
    is the only thing in the template that names daniel-pi — the Pi runs no kubelet, so it
    can never appear via the pod-discovery jobs, only via a STATIC target (see the "daniel-pi,
    the one host outside the cluster" comment in the template itself).
    """
    names, pending = set(), None
    for line in text.splitlines():
        m = _JOB_NAME_RE.match(line)
        if m:
            pending = m.group(1)
            continue
        if "k8s_pi_client_ip" in line and pending:
            names.add(pending)
    return names


def format_pi_targets(declared, active_targets):
    """Summarize daniel-pi's Prometheus scrape targets. Pure. Returns (text, exit_code).

    `declared` is the job-name set from declared_pi_job_names(); `active_targets` is
    Prometheus's own `data.activeTargets` list. A declared job absent from the live set is
    reported MISSING and fails the gate — dividing the Pi's own live set by itself (the
    `monitors` mistake `kuma-drift` exists to not repeat) would read a scrape config with a
    silently dropped job as 100% healthy.

    glances is deliberately not in `declared`: it has no Prometheus job anywhere in this repo
    (probe.py polls its own JSON API directly, at `pi <subpath>`) — that is a fact about the
    scrape config, not a gap this check should paper over by inventing one.
    """
    live = {
        t.get("scrapePool"): t
        for t in active_targets
        if (t.get("labels") or {}).get("origin") == PI_ORIGIN_LABEL
        or (t.get("labels") or {}).get("instance") == PI_ORIGIN_LABEL
    }
    missing = sorted(declared - live.keys())
    orphans = sorted(live.keys() - declared)
    lines = []
    for name in sorted(declared):
        target = live.get(name)
        if target is None:
            lines.append(
                f"  {name}: MISSING — declared in prometheus.yaml.j2, not scraping"
            )
            continue
        health = target.get("health", "unknown")
        if health == "up":
            lines.append(f"  {name}: up")
        else:
            err = target.get("lastError") or "no error given"
            lines.append(f"  {name}: {health} — {err}")
    for name in orphans:
        lines.append(
            f"  {name}: live, not declared (job renamed in prometheus.yaml.j2?)"
        )
    down = [
        name for name, t in live.items() if name in declared and t.get("health") != "up"
    ]
    up = len(declared) - len(missing) - len(down)
    head = f"{up}/{len(declared)} daniel-pi targets up"
    note = (
        "  (glances is polled directly via `probe.py pi <path>`, not scraped by "
        "Prometheus — no job declares it)"
    )
    text = "\n".join([head, *lines, note])
    return text, 1 if missing or down else 0


def run_pi_targets(ns):
    """`probe.py targets --pi` (exit 0 = every declared Pi job is up)."""
    base, pin = prom_endpoint()
    url = prom_targets_url(base)
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)
    declared = declared_pi_job_names(PROMETHEUS_TEMPLATE_PATH.read_text())
    data, err = core.fetch_json(url, resolve=pin)
    if err:
        return err
    active = (data.get("data") or {}).get("activeTargets") or []
    text, code = format_pi_targets(declared, active)
    print(text)
    return code


# pi containers: one ssh call, `docker ps -a` piped straight into `docker inspect`.
#
# ONE ssh invocation total — daniel-pi sits behind `ufw limit ssh`, which REJECTs the 6th+
# connection in a 30s window (MEMORY: ssh-rate-limit-refuses-rapid-agent-calls.md). A
# ps-then-inspect round trip from the CALLER side would be two; running both commands in the
# one remote shell this ssh opens keeps it to one.
#
# Measured against the live Pi (2026-09-03, 7 containers, six consecutive runs): 3.65s, 7.8s,
# 8.3s, 12.8s, 14.5s, 19.3s — almost entirely ssh/exec overhead on a Zero 2 W under its own
# cron load, not the inspect itself. A 25s timeout was too tight and timed out on a seventh
# run. PI_CONTAINERS_TIMEOUT sits well above the observed tail rather than at core.py's 10s
# HTTP default, which this is not.
PI_CONTAINERS_TIMEOUT = 45


def pi_containers_argv():
    return ["ssh", PI_HOST, "docker inspect $(docker ps -aq)"]


def format_pi_containers(containers):
    """Summarize every container on daniel-pi: name, image, state, health, networks. Pure.

    Returns (text, exit_code). Flags only what the caller asked for — a running container with
    no network at all (the reboot-detach failure MEMORY records: `Up (healthy)` with
    `Networks={}`) and an unhealthy healthcheck. A merely-stopped container is not flagged: this
    host runs a docker-proxy-lifecycle sub-proxy and other short-lived helpers, and gating the
    exit code on ANY non-running container would make this red on a normal day and get ignored.
    """
    if not containers:
        return "no containers on daniel-pi (docker ps -a returned nothing)", 1
    flagged, lines = [], []
    for c in sorted(containers, key=lambda c: c.get("Name") or ""):
        name = (c.get("Name") or "?").lstrip("/")
        image = (c.get("Config") or {}).get("Image", "?")
        state = c.get("State") or {}
        status = state.get("Status", "unknown")
        health = (state.get("Health") or {}).get("Status")
        networks = sorted(
            ((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()
        )
        problems = []
        if status == "running" and not networks:
            problems.append("DETACHED — running with no network")
        if health and health != "healthy":
            problems.append(f"health={health}")
        line = f"  {name}: {image}, status={status}"
        if health:
            line += f", health={health}"
        line += f", networks={','.join(networks) or 'none'}"
        if problems:
            line += f" — {'; '.join(problems)}"
            flagged.append(name)
        lines.append(line)
    if flagged:
        head = f"{len(flagged)} of {len(containers)} containers flagged — {', '.join(flagged)}"
    else:
        head = f"all {len(containers)} containers clean"
    return "\n".join([head, *lines]), 1 if flagged else 0


def run_pi_containers(ns):
    """`probe.py pi containers` (exit 0 = no detached or unhealthy container)."""
    argv = pi_containers_argv()
    if ns.dry_run:
        print(" ".join(argv))
        return 0
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=PI_CONTAINERS_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        print(f"ssh {PI_HOST} docker inspect timed out after {PI_CONTAINERS_TIMEOUT}s")
        return 1
    if out.returncode != 0:
        print(f"ssh {PI_HOST} docker inspect failed: {out.stderr.strip()}")
        return 1
    try:
        containers = json.loads(out.stdout)
    except json.JSONDecodeError:
        print(f"ssh {PI_HOST} docker inspect returned unparseable output")
        return 1
    text, code = format_pi_containers(containers)
    print(text)
    return code
