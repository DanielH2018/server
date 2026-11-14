"""`probe.py usage` -- how the services are used, read from Prometheus.

Three tables:

1. Requests per service over the whole retention window, summed across the service's
   IngressRoutes. Traefik labels each route `<namespace>-<route>-<hash>@kubernetescrd`, and a
   route is named `<service>[-public|-monitoring|-push-monitoring|-read|-public-<path>]`, so
   the service is the name up to the first `ROUTE_WORDS` segment.
2. Each container's memory request, limit and 7-day p95 working set.
3. Each PVC's fill and its projected days to full.

A report, not a verdict: it exits 0 once every query answered, and 75 when one did not.
"""

import re

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path; pyproject's `pythonpath` is a pytest setting. This has to sit
# ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from diagnostics.probe_lib.health_alerts import prom_query
from diagnostics.probe_lib.metrics import SPAN_PROMQL, format_span

ROUTE_WORDS = frozenset({"public", "monitoring", "push", "read"})
KINDS = ("lan", "public", "api", "monitoring")
_CRD_HASH = re.compile(r"-[0-9a-f]{20}$")

NAMESPACES_QUERY = "group by (namespace) (kube_pod_info)"


def requests_query(window_s):
    """Each Traefik service's request count over the last `window_s` seconds."""
    return (
        f"sum by (service) (increase(traefik_service_requests_total[{int(window_s)}s]))"
    )


MEMORY_WINDOW = "7d"
P95_QUERY = (
    "max by (namespace, pod, container) (quantile_over_time(0.95, "
    'container_memory_working_set_bytes{container!="",container!="POD"}'
    f"[{MEMORY_WINDOW}]))"
)
REQUEST_QUERY = (
    "max by (namespace, pod, container) "
    '(kube_pod_container_resource_requests{resource="memory"}) '
    '* on (namespace, pod) group_left () (kube_pod_status_phase{phase="Running"} == 1)'
)
LIMIT_QUERY = REQUEST_QUERY.replace("requests", "limits")
NEAR_LIMIT = 0.80
REQUEST_SLACK = 0.25

_PVC = "max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_{}_bytes)"
USED_QUERY = _PVC.format("used")
CAPACITY_QUERY = _PVC.format("capacity")
GROWTH_QUERY = f"deriv({USED_QUERY}[7d:1h])"
PVC_FULL = 0.80
PVC_HORIZON_DAYS = 90

# Kubernetes' generated-name alphabet has no vowels and no 0, 1 or 3.
_GEN = "[bcdfghjklmnpqrstvwxz2456789]"
_WORKLOAD_SUFFIXES = (
    re.compile(rf"-{_GEN}{{6,10}}-{_GEN}{{5}}$"),  # Deployment pod
    re.compile(rf"-\d{{8,}}-{_GEN}{{5}}$"),  # CronJob pod
    re.compile(rf"-{_GEN}{{5}}$"),  # DaemonSet or Job pod
    re.compile(r"-\d+$"),  # StatefulSet pod
)


def route_service(label: str, namespaces: set[str]):
    """`(service, kind)` for a Traefik `service` label, or None for Traefik's own. Pure.

    `kind` is one of `KINDS`: `monitoring` for a route with a `monitoring` segment, `public`
    for `<service>-public`, `lan` for the bare `<service>`, and `api` for any other suffix,
    such as `-public-api-trpc` or `-read`.
    """
    name, _, provider = label.partition("@")
    if provider == "internal":
        return None
    if provider == "kubernetescrd":
        name = _CRD_HASH.sub("", name)
        for ns in sorted(namespaces, key=len, reverse=True):
            if name.startswith(ns + "-"):
                name = name[len(ns) + 1 :]
                break
    parts = name.split("-")
    cut = next((i for i, p in enumerate(parts) if i and p in ROUTE_WORDS), len(parts))
    rest = parts[cut:]
    if "monitoring" in rest:
        kind = "monitoring"
    elif rest == ["public"]:
        kind = "public"
    elif not rest:
        kind = "lan"
    else:
        kind = "api"
    return "-".join(parts[:cut]), kind


def _value(series):
    return float(series["value"][1])


def format_requests(series, namespaces, window_s):
    """The requests table, one row per service, busiest first. Pure.

    `total` sums the LAN, public and API routes; the monitoring routes carry probe traffic,
    so they sit in their own column and stay out of the total.
    """
    counts = {}
    for s in series:
        parsed = route_service(s.get("metric", {}).get("service", ""), namespaces)
        if parsed is None:
            continue
        service, kind = parsed
        row = counts.setdefault(service, dict.fromkeys(KINDS, 0.0))
        row[kind] += _value(s)
    width = max([len("service"), *map(len, counts)]) + 2
    lines = [
        f"Requests: {len(counts)} services over the last {format_span(window_s)}, the "
        "whole Prometheus retention window; `total` excludes the monitoring routes",
        f"  {'service':<{width}}{'total':>10}{'lan':>10}{'public':>10}{'api':>10}"
        f"{'monitoring':>12}",
    ]
    rows = sorted(
        counts.items(),
        key=lambda kv: (-(kv[1]["lan"] + kv[1]["public"] + kv[1]["api"]), kv[0]),
    )
    for service, row in rows:
        total = row["lan"] + row["public"] + row["api"]
        cells = "".join(f"{round(row[k]):>10}" for k in KINDS[:3])
        lines.append(
            f"  {service:<{width}}{round(total):>10}{cells}{round(row['monitoring']):>12}"
        )
    return lines


def workload(pod):
    """The workload a pod name belongs to, its generated suffix removed. Pure."""
    for suffix in _WORKLOAD_SUFFIXES:
        stripped = suffix.sub("", pod)
        if stripped != pod:
            return stripped
    return pod


def _by_container(series):
    """`{(namespace, workload, container): max value}` over every pod of the workload."""
    out = {}
    for s in series:
        m = s.get("metric", {})
        key = (
            m.get("namespace", "?"),
            workload(m.get("pod", "?")),
            m.get("container", "?"),
        )
        out[key] = max(out.get(key, 0.0), _value(s))
    return out


def _mib(value):
    return "-" if value is None else f"{value / 2**20:.0f}Mi"


def memory_flags(request, limit, p95):
    """The flags a container's row carries: `near limit` and `request off`. Pure."""
    flags = []
    if p95 is None:
        return flags
    if limit and p95 > NEAR_LIMIT * limit:
        flags.append("near limit")
    if request and abs(p95 - request) > REQUEST_SLACK * request:
        flags.append("request off")
    return flags


def format_memory(p95_series, request_series, limit_series, show_all=False):
    """The memory table: a count line, then the flagged containers. Pure.

    A row is a container a Running pod declares a request or a limit for. Its p95 is the
    highest p95 of any pod of the same workload in the window, so a rollout does not reset it.
    `show_all` lists the unflagged rows too.
    """
    p95s = _by_container(p95_series)
    requests = _by_container(request_series)
    limits = _by_container(limit_series)
    rows = []
    for key in set(requests) | set(limits):
        req, lim, p95 = requests.get(key), limits.get(key), p95s.get(key)
        rows.append((memory_flags(req, lim, p95), key, req, lim, p95))
    rows.sort(key=lambda r: ("near limit" not in r[0], not r[0], r[1]))
    width = max(
        [len("namespace/workload/container"), *(len("/".join(r[1])) for r in rows)]
    )
    width += 2
    flagged = sum(1 for r in rows if r[0])
    hint = "" if show_all else "; --all to list every container"
    lines = [
        f"Memory: {len(rows)} containers, {flagged} flagged against their {MEMORY_WINDOW} "
        f"p95 working set (near limit = p95 above {NEAR_LIMIT:.0%} of the limit, request "
        f"off = p95 more than {REQUEST_SLACK:.0%} from the request){hint}",
        f"  {'namespace/workload/container':<{width}}{'request':>9}{'limit':>9}{'p95':>9}  flags",
    ]
    for flags, key, req, lim, p95 in rows:
        if flags or show_all:
            row = f"  {'/'.join(key):<{width}}{_mib(req):>9}{_mib(lim):>9}{_mib(p95):>9}  "
            lines.append((row + ", ".join(flags)).rstrip())
    return lines


def _by_pvc(series):
    out = {}
    for s in series:
        m = s.get("metric", {})
        out[(m.get("namespace", "?"), m.get("persistentvolumeclaim", "?"))] = _value(s)
    return out


def days_to_full(used, capacity, growth_per_s):
    """Days until `used` reaches `capacity` at `growth_per_s`, None when not growing. Pure."""
    if growth_per_s is None or growth_per_s <= 0:
        return None
    return (capacity - used) / growth_per_s / 86400


def pvc_needs_attention(fill, days):
    """Whether a PVC is over `PVC_FULL` or fills inside `PVC_HORIZON_DAYS`. Pure."""
    return fill > PVC_FULL or (days is not None and days < PVC_HORIZON_DAYS)


def format_pvcs(used_series, capacity_series, growth_series, show_all=False):
    """The PVC table: a count line, then the PVCs that need attention, fullest first. Pure."""
    used, capacity, growth = (
        _by_pvc(used_series),
        _by_pvc(capacity_series),
        _by_pvc(growth_series),
    )
    rows = []
    for key in used:
        if capacity.get(key):
            days = days_to_full(used[key], capacity[key], growth.get(key))
            rows.append((used[key] / capacity[key], key, days))
    rows.sort(key=lambda r: (-r[0], r[1]))
    width = max([len("namespace/pvc"), *(len("/".join(r[1])) for r in rows)]) + 2
    shown = [r for r in rows if show_all or pvc_needs_attention(r[0], r[2])]
    hint = "" if show_all else "; --all to list every PVC"
    lines = [
        f"PVCs: {len(rows)}, {sum(1 for r in rows if pvc_needs_attention(r[0], r[2]))} "
        f"over {PVC_FULL:.0%} full or full inside {PVC_HORIZON_DAYS} days at the 7d growth "
        f"rate{hint}",
        f"  {'namespace/pvc':<{width}}{'fill':>7}{'days to full':>14}",
    ]
    for fill, key, days in shown:
        when = "-" if days is None else f"{days:.0f}"
        lines.append(f"  {'/'.join(key):<{width}}{fill:>7.1%}{when:>14}")
    return lines


def report(query=prom_query, show_all=False):
    """The three tables as lines.

    Args:
        query: returns an instant query's result list; raises `core.Unreadable` on a failed
            read.
        show_all: list every container and PVC, not just the flagged ones.

    Raises:
        core.Unreadable: a query failed, or Prometheus reported no retention span.
    """
    span = query(SPAN_PROMQL)
    if not span:
        raise core.Unreadable(f"Prometheus returned no series for {SPAN_PROMQL}")
    window_s = _value(span[0])
    namespaces = {s["metric"]["namespace"] for s in query(NAMESPACES_QUERY)}
    lines = format_requests(query(requests_query(window_s)), namespaces, window_s)
    lines.append("")
    lines += format_memory(
        query(P95_QUERY), query(REQUEST_QUERY), query(LIMIT_QUERY), show_all
    )
    lines.append("")
    lines += format_pvcs(
        query(USED_QUERY), query(CAPACITY_QUERY), query(GROWTH_QUERY), show_all
    )
    return lines


def run_usage(ns):
    """Print the usage report; 0 once every query answered."""
    print("\n".join(report(show_all=ns.all)))
    return 0
