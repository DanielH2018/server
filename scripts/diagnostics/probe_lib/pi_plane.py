"""`probe.py targets [--pi]` and `probe.py pi containers` — scrape targets and daniel-pi triage.

Plain `targets` prints each down target and the up count; `targets --json` streams the raw
Prometheus JSON through `plan()` instead. A down target whose job names a Deployment at
`spec.replicas: 0`, such as an idle game server's stats exporter, prints as `scaled to 0` and
does not fail the verdict. daniel-pi runs no kubelet, so `--pi` scopes the view
to the jobs observability's prometheus.yaml.j2 declares on it.

`alerts --pi` filters in probe_lib/alerts.py, beside the series it reads, and takes its job
set from `declared_pi_job_names` here. This module carries the two things that have no other
home: Prometheus target formatting and the Pi's own Docker container inspect.
"""

import json
import re
import subprocess
from collections.abc import Collection

import sys as _sys


# Use `core.<name>` for anything tests monkeypatch: a `from core import` binds a snapshot the patch
# never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import (
    PI_HOST,
    pi_ssh_argv,
    prom_endpoint,
    prom_targets_url,
)

from lib.exit_codes import PROBE_UNREADABLE
from lib.kubectl import DEFAULT_CLUSTER, MissingKubectl, WrongCluster, kubectl_json
from lib.repo_paths import REPO

PROMETHEUS_TEMPLATE_PATH = (
    REPO / "k8s" / "roles" / "observability" / "templates" / "prometheus.yaml.j2"
)

# prometheus.yaml.j2 writes `origin: daniel-pi` literally, so the label equals the host name by
# convention only.
PI_ORIGIN_LABEL = PI_HOST

_JOB_NAME_RE = re.compile(r"^\s*-\s*job_name:\s*(\S+)")


def declared_pi_job_names(text):
    """Prometheus job names whose static target is daniel-pi's LAN IP.

    Parses observability's prometheus.yaml.j2 rather than hand-listing "node-pi"/"alloy-pi": a
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
    reported MISSING and fails the gate — dividing the Pi's own live set by itself would read
    a scrape config with a silently dropped job as 100% healthy.
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
    head = f"{up}/{len(declared)} {PI_HOST} targets up"
    text = "\n".join([head, *lines])
    return text, 1 if missing or down else 0


def format_targets(active, scaled_to_zero: Collection[str] = frozenset()):
    """(text, exit code) for every scrape target: the count, then one line per down target.

    A down target whose job is in `scaled_to_zero` prints as `scaled to 0` and does not fail.
    """
    down = [t for t in active if t.get("health") != "up"]
    lines, failed = [], 0
    for t in down:
        labels = t.get("labels") or {}
        job, inst = labels.get("job", "?"), labels.get("instance", "?")
        if job in scaled_to_zero:
            lines.append(f"  - {job} [{inst}] scaled to 0")
            continue
        failed += 1
        err = " ".join((t.get("lastError") or "no error given").split())[:120]
        lines.append(f"  ✗ {job} [{inst}] {err}")
    count = f"{len(active) - len(down)}/{len(active)} targets up"
    if len(down) > failed:
        count += f", {len(down) - failed} scaled to 0"
    return "\n".join([count, *lines]), 1 if failed else 0


def scaled_to_zero_deployments(cluster, read=kubectl_json):
    """Names of the workload namespace's Deployments that declare `spec.replicas: 0`.

    Any failed read returns an empty set, so a down target nobody can show to be idle on
    purpose keeps failing the verdict. `read` replaces `kubectl_json`, for a test.
    """
    try:
        data = read(
            cluster, "-n", core.k8s_namespace(), "get", "deployments", "-o", "json"
        )
    except WrongCluster, MissingKubectl, subprocess.TimeoutExpired, OSError:
        return set()
    items = (data or {}).get("items")
    names = set()
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict):
            continue
        meta, spec = d.get("metadata"), d.get("spec")
        if (
            isinstance(meta, dict)
            and isinstance(spec, dict)
            and spec.get("replicas") == 0
        ):
            names.add(meta.get("name"))
    return names


def run_targets(ns, fetch_json=None, scaled_lookup=scaled_to_zero_deployments):
    """`probe.py targets` (exit 0 = every target is up or scaled to 0); `--pi` scopes it to daniel-pi.

    `fetch_json` replaces `core.fetch_json` and `scaled_lookup` the live Deployment read, for a
    test. The Deployment read runs only when a target is down.
    """
    if ns.pi:
        return run_pi_targets(ns)
    base, pin = prom_endpoint()
    data, err = (fetch_json or core.fetch_json)(prom_targets_url(base), resolve=pin)
    if data is None:
        return err or PROBE_UNREADABLE
    active = (data.get("data") or {}).get("activeTargets") or []
    scaled = set()
    if any(t.get("health") != "up" for t in active):
        scaled = scaled_lookup(getattr(ns, "cluster", DEFAULT_CLUSTER))
    text, code = format_targets(active, scaled)
    print(text)
    return code


def run_pi_targets(ns):
    """`probe.py targets --pi` (exit 0 = every declared Pi job is up)."""
    base, pin = prom_endpoint()
    url = prom_targets_url(base)
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)
    declared = declared_pi_job_names(PROMETHEUS_TEMPLATE_PATH.read_text())
    data, err = core.fetch_json(url, resolve=pin)
    if data is None:
        return err or PROBE_UNREADABLE
    active = (data.get("data") or {}).get("activeTargets") or []
    text, code = format_pi_targets(declared, active)
    print(text)
    return code


# ONE ssh invocation: daniel-pi's `ufw limit ssh` REJECTs the 6th connection in 30s.
# Timeout sits above the observed tail (3.65s to 19.3s on a Zero 2 W).
PI_CONTAINERS_TIMEOUT = 45


def pi_containers_argv(hostname=None):
    return pi_ssh_argv("docker inspect $(docker ps -aq)", hostname)


def format_pi_containers(containers):
    """Summarize every container on daniel-pi: name, image, state, health, networks. Pure.

    Returns (text, exit_code). Flags only what the caller asked for — a running container with
    no network at all (the reboot-detach failure MEMORY records: `Up (healthy)` with
    `Networks={}`) and an unhealthy healthcheck. A merely-stopped container is not flagged: this
    host runs a docker-proxy-lifecycle sub-proxy and other short-lived helpers, and gating the
    exit code on ANY non-running container would make this red on a normal day and get ignored.
    """
    if not containers:
        return f"no containers on {PI_HOST} (docker ps -a returned nothing)", 1
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
        print(
            f"ssh {PI_HOST} docker inspect timed out after {PI_CONTAINERS_TIMEOUT}s",
            file=_sys.stderr,
        )
        return PROBE_UNREADABLE
    if out.returncode != 0:
        print(
            f"ssh {PI_HOST} docker inspect failed: {out.stderr.strip()}",
            file=_sys.stderr,
        )
        return PROBE_UNREADABLE
    try:
        containers = json.loads(out.stdout)
    except json.JSONDecodeError:
        print(
            f"ssh {PI_HOST} docker inspect returned unparseable output",
            file=_sys.stderr,
        )
        return PROBE_UNREADABLE
    text, code = format_pi_containers(containers)
    print(text)
    return code
