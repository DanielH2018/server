"""The alert-plane half of `probe.py health <tag>`: firing alerts and ingress probes.

`run_health` calls `gate` after the rollout rollup passes (M6 B-1,
docs/target-architecture/monitoring/m6-retire-kuma.md *Phase B*, item 1). The selector is
C7's *Health gate* in docs/target-architecture/contracts.md, with its six departures from the
literal `ALERTS{alertstate="firing",service="<tag>"}`. D5's soak reads the same set, so a
change to `gated_selectors` is a contract change, not a local tweak.

Two halves, each judged against a reference time:

  - Alerts. A firing alert in the gated set fails the gate only when it became active
    (`ALERTS_FOR_STATE`) after the tag's reference time: `applied_at` for an Ansible-owned
    tag, the time its Kustomization first reported its current revision for an `owner: flux`
    tag. With no reference time every gated alert fails. Older alerts, and firing alerts
    outside the gated set, print as notes.
  - Probes. The newest `probe_success{service="<tag>"}` sample of each target must postdate
    the rollout, and read 1. A tag whose entry renders no ingress route skips this half.

A would-fail result is re-read once after `REREAD_WAIT_S`, because a probe alert that fired
during a slow rollout can still read firing at the first read.

Every read goes through Prometheus' query-only IngressRoute, which admits `/api/v1/query`
alone. A read that fails raises `core.Unreadable` (exit 75), never a pass.
"""

import calendar
import time
from dataclasses import dataclass
from typing import Callable

# `probe_lib` is a namespace package under `scripts/`, as in health.py.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from diagnostics.probe_lib.obs_api import prom_query_url

from lib.repo_paths import FILTER_PLUGINS


# C7 *Health gate*: `shadow!="true"`, `alertname!="Watchdog"`, and `severity="critical"` or
# `verify="true"` (the soak's set, C7 *Verify successors* shape 1).
def note_selector(tag):
    """Every firing alert of `tag` the gate may name, gated or only noted."""
    return (
        f'ALERTS{{alertstate="firing",service="{tag}",shadow!="true",'
        'alertname!="Watchdog"}'
    )


def gated_selectors(tag):
    """The two selectors whose union is C7's gated set for `tag`."""
    base = note_selector(tag)[:-1]
    return (base + ',severity="critical"}', base + ',verify="true"}')


# Rules evaluate every minute (prometheus.yaml.j2 sets no evaluation_interval). An alert that
# was already firing when Prometheus started reads an activation within one evaluation of the
# start: 6.4s after it on 2026-10-16 for the Watchdog, which had fired throughout.
RESTART_SLACK_S = 120

# One 1m scrape interval plus slack, the plan's bound on waiting for a fresh probe sample.
PROBE_WAIT_S = 90
POLL_S = 10
REREAD_WAIT_S = 60

# The flux reference reads back this far. Prometheus retains about a week, so a revision first
# reported before the window reads as first reported at its start. That moves the reference
# later, which gates more alerts, never fewer.
FLUX_LOOKBACK = "7d"


def prom_query(promql, endpoint=None, fetch=None):
    """The `result` list of one instant query.

    `endpoint` and `fetch` default to `core.prom_endpoint` and `core.fetch_parsed`, read at
    call time; a test passes its own.

    Raises:
        core.Unreadable: curl failed, or Prometheus did not answer `success`.
    """
    base, pin = (endpoint or core.prom_endpoint)()
    url = prom_query_url(base, promql)
    data = (fetch or core.fetch_parsed)(url, resolve=pin)
    if not isinstance(data, dict) or data.get("status") != "success":
        raise core.Unreadable(f"Prometheus refused {promql}: {str(data)[:300]}")
    return (data.get("data") or {}).get("result") or []


@dataclass(frozen=True)
class PromTools:
    """The boundaries `gate` crosses, so a test replaces a field rather than patching."""

    query: Callable[[str], list] = prom_query
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time


DEFAULT_PROM = PromTools()


def _labels(series, drop=("__name__",)):
    return {k: v for k, v in (series.get("metric") or {}).items() if k not in drop}


def _key(series):
    # ALERTS_FOR_STATE carries every label ALERTS does except `alertstate`.
    return frozenset(_labels(series, ("__name__", "alertstate")).items())


def _name(series):
    labels = _labels(series)
    return f"{labels.get('alertname', '?')}{{severity={labels.get('severity', '?')}}}"


def _when(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def flux_reference(tag, query):
    """When `tag`'s Kustomization first reported its current revision, or None with no series.

    `gotk_resource_info` (C6) is one series per (revision, ready) pair, so the earliest sample
    of any series carrying the current revision is the first report.
    """
    selector = f'gotk_resource_info{{customresource_kind="Kustomization",name="{tag}"}}'
    rows = query(
        f"min(min_over_time(timestamp({selector})[{FLUX_LOOKBACK}:1m]) "
        f"and on(revision) {selector})"
    )
    return float(rows[0]["value"][1]) if rows else None


def judge_alerts(tag, reference, query):
    """(failing lines, note lines) for the firing alerts of `tag` against `reference`."""
    gated = {}
    for selector in gated_selectors(tag):
        for series in query(selector):
            gated[_key(series)] = series
    activations = {
        _key(s): float(s["value"][1])
        for s in query(f'ALERTS_FOR_STATE{{service="{tag}"}}')
    }
    since = "no reference time" if reference is None else f"after {_when(reference)}"
    failing, notes = (
        [],
        [f"  alerts: {len(gated)} gated alert(s) firing, judged {since}"],
    )
    prom_start = None
    for key, series in sorted(gated.items(), key=lambda kv: _name(kv[1])):
        active = activations.get(key)
        if reference is not None and active is not None and active <= reference:
            notes.append(
                f"  note: {_name(series)} firing since {_when(active)}, before the "
                f"reference {_when(reference)}"
            )
            continue
        if active is None:
            failing.append(f"  {_name(series)} firing, activation time unreadable")
            continue
        if prom_start is None:
            rows = query('process_start_time_seconds{job="prometheus"}')
            prom_start = float(rows[0]["value"][1]) if rows else float("nan")
        if active <= prom_start + RESTART_SLACK_S:
            failing.append(
                f"  {_name(series)} firing, active since Prometheus started at "
                f"{_when(prom_start)}, so its age is unknown"
            )
        else:
            failing.append(f"  {_name(series)} firing since {_when(active)}")
    for series in query(note_selector(tag)):
        if _key(series) not in gated:
            notes.append(f"  note: {_name(series)} firing, outside the gated set")
    return failing, notes


def judge_probes(tag, reference, tools):
    """(failing lines, note lines) for `tag`'s ingress probes, waiting for a fresh sample.

    `reference` is when the rollout completed. A sample at or before it may be the old pod's
    1 or a mid-rollout 0, so it decides nothing.
    """
    selector = f'probe_success{{service="{tag}"}}'
    deadline = tools.clock() + PROBE_WAIT_S
    while True:
        values = {_key(s): s for s in tools.query(selector)}
        if not values:
            # Only a routed tag reaches here, and its route renders a blackbox target, so no
            # series means the probe stopped reporting: blackbox-exporter down, or the job
            # broken. A route added since `observability` last deployed reads the same way.
            if tools.clock() >= deadline:
                return [
                    f"  {tag} has an ingress route but no probe_success series after "
                    f"{PROBE_WAIT_S}s (a new route needs an `observability` deploy)"
                ], []
            tools.sleep(POLL_S)
            continue
        stamps = {
            _key(s): float(s["value"][1]) for s in tools.query(f"timestamp({selector})")
        }
        stale = [k for k in values if stamps.get(k, 0.0) <= reference]
        if not stale:
            break
        if tools.clock() >= deadline:
            return [
                f"  probe {dict(k).get('instance', '?')} reported no sample after "
                f"{_when(reference)} within {PROBE_WAIT_S}s"
                for k in stale
            ], []
        tools.sleep(POLL_S)
    failing, notes = [], []
    for k, s in values.items():
        line = (
            f"  probe {dict(k).get('instance', '?')} reads probe_success "
            f"{s['value'][1]} at {_when(stamps[k])}"
        )
        (notes if float(s["value"][1]) == 1 else failing).append(line)
    return failing, notes


def gate(tag, reference, probe_reference, has_route, tools=DEFAULT_PROM):
    """(exit code, lines) for the alert plane of `tag`.

    Args:
        tag: The deploy tag, which is every rule's and probe target's `service` label.
        reference: The alert reference time in epoch seconds, or None when the tag has none.
        probe_reference: When the rollout completed, in epoch seconds.
        has_route: Whether the tag's rendered entry has an ingress route.
        tools: The Prometheus read, sleep and clock.

    Raises:
        core.Unreadable: a Prometheus read failed.
    """

    def judge():
        failing, notes = judge_alerts(tag, reference, tools.query)
        if has_route:
            probe_failing, probe_notes = judge_probes(tag, probe_reference, tools)
        else:
            probe_failing = []
            probe_notes = [
                f"  note: {tag} renders no ingress route, probe half skipped"
            ]
        return failing + probe_failing, notes + probe_notes

    failing, notes = judge()
    if failing:
        tools.sleep(REREAD_WAIT_S)
        failing, notes = judge()
    return (1 if failing else 0), failing + notes


def epoch(stamp):
    """An RFC3339 `...Z` timestamp as epoch seconds, or None when it does not parse."""
    try:
        return calendar.timegm(time.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError, TypeError:
        return None


def pods_ready_at(pods_docs):
    """The newest Ready transition across the pods in `pods_docs`, in epoch seconds, or None."""
    times = [
        epoch(cond.get("lastTransitionTime"))
        for doc in pods_docs
        for pod in (doc or {}).get("items") or []
        for cond in (pod.get("status") or {}).get("conditions") or []
        if cond.get("type") == "Ready" and cond.get("status") == "True"
    ]
    times = [t for t in times if t is not None]
    return max(times) if times else None


def has_ingress_route(entry):
    """Whether `entry` renders an ingress probe target, as prometheus.yaml.j2 does.

    The blackbox job loops over `containers_list | kuma_ingress_monitors`, so the same filter
    applied to this one entry is the render. An entry whose route was removed keeps an old
    `probe_success` series inside the lookback, which must not read as a stopped probe.
    """
    if entry is None:
        return False
    _sys.path.insert(0, str(FILTER_PLUGINS))
    from kuma_monitors import kuma_ingress_monitors

    return bool(kuma_ingress_monitors([entry]))


def alert_reference(tag, owner, applied_at, query):
    """The time after which a firing alert of `tag` counts against this deploy (C7).

    `applied_at` (the release record's stamp) for an Ansible-owned tag. For an `owner: flux`
    tag the release record froze at the last Ansible apply, so the reference is when its
    Kustomization first reported its current revision. None when neither is readable, and
    then every gated alert fails.
    """
    if owner == "flux":
        return flux_reference(tag, query)
    if owner is not None:
        return None
    return epoch(applied_at)


def check(tag, owner, applied_at, ready_at, entry, tools=DEFAULT_PROM):
    """`gate` for `tag`, with both reference times derived from its ownership and rollout.

    The probe reference is the later of the alert reference and the newest pod Ready
    transition: a sample before it may be the old pod's 1 or a mid-rollout 0.
    """
    reference = alert_reference(tag, owner, applied_at, tools.query)
    rolled = [t for t in (reference, ready_at) if t is not None]
    probe_reference = max(rolled) if rolled else tools.clock()
    return gate(tag, reference, probe_reference, has_ingress_route(entry), tools)
