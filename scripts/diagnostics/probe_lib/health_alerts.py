"""The alert-plane half of `probe.py health <tag>`: firing alerts and ingress probes.

`run_health` calls `gate` after the rollout rollup passes (M6 B-1,
docs/target-architecture/monitoring/m6-retire-kuma.md *Phase B*, item 1). The selector is
C7's *Health gate* in docs/target-architecture/contracts.md, with its six departures from the
literal `ALERTS{alertstate="firing",service="<tag>"}`. D5's soak reads the same set, so a
change to `gated_selectors` is a contract change, not a local tweak.

Two halves, each judged against a reference time:

  - Alerts. A firing alert in the gated set fails the gate only when it became active
    (`ALERTS_FOR_STATE`) after the tag's reference time: the time its Kustomization first
    reported its current revision. With no reference time every gated
    alert fails. Older alerts, and firing alerts
    outside the gated set, print as notes.
  - Probes. The newest `probe_success{service="<tag>"}` sample of each target must postdate
    the rollout, and read 1. A tag whose entry renders no ingress route skips this half, and
    so does a route Prometheus has not loaded a probe target for yet (`unprobed_route`).

A would-fail result is re-read once after `REREAD_WAIT_S`, because a probe alert that fired
during a slow rollout can still read firing at the first read. Every wait ends inside
`PROCESS_DEADLINE`, so a plumbing check's settle wait shortens the probe polls rather than
pushing the process past `detach_notify.PROBE_TIMEOUT_S`. A re-read that cannot fit raises
`core.Unreadable`, because the would-fail read it was meant to confirm stays unconfirmed.

Every read goes through Prometheus' query-only IngressRoute, which admits `/api/v1/query`
alone. A read that fails raises `core.Unreadable` (exit 75), never a pass. One read goes to
Alertmanager instead, because `ALERTS` shows no silence or inhibition: `alertmanager_api` names
the one muting a gated alert, and its failure prints a note rather than changing the verdict.
"""

import calendar
import time
from dataclasses import dataclass
from typing import Callable

# `probe_lib` is a namespace package under `scripts/`, as in health.py.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import alertmanager_api, core
from diagnostics.probe_lib.obs_api import prom_query_url


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


# The query door's Traefik `rate-limit` is 300/min per client, and every gate on a host shares
# one client address, so concurrent landings drain it together (#7721).
RATE_LIMITED = 429
RETRY_DELAYS_S = (2, 4, 8, 16)
# `detach_notify.PROBE_TIMEOUT_S`, restated: probe_lib may not import deploy_tools. Its expiry
# grades a tag unhealthy rather than unread, so every wait in a probe process ends before it.
PROBE_TIMEOUT_S = 300
# Room after the last wait for the reads that follow it and the process exit.
READ_MARGIN_S = 15
# A 429 retry may sleep only while every wait that can still follow one (the rest of a probe
# poll, the re-read, a second poll) fits after it.
RETRY_CUTOFF_S = (
    PROBE_TIMEOUT_S - (PROBE_WAIT_S + REREAD_WAIT_S + PROBE_WAIT_S) - READ_MARGIN_S
)


class RetryWindow:
    """The span from a probe process's start in which a sleep may still end.

    Attributes:
        cutoff_s: the seconds after `start` by which a sleep must end.
        clock: a monotonic clock in seconds.
        start: the clock reading the span opens at.
    """

    def __init__(self, cutoff_s, clock=time.monotonic, start=None):
        self.cutoff_s = cutoff_s
        self.clock = clock
        self.start = clock() if start is None else start

    def allows(self, delay):
        """True when a sleep of `delay` seconds from now ends inside the window."""
        return self.clock() - self.start + delay <= self.cutoff_s

    def remaining(self):
        """The seconds left before the window closes, never below 0."""
        return max(0.0, self.cutoff_s - (self.clock() - self.start))


PROCESS_WINDOW = RetryWindow(RETRY_CUTOFF_S)
PROCESS_DEADLINE = RetryWindow(PROBE_TIMEOUT_S - READ_MARGIN_S)


def _get_json(url, resolve, get, sleep, window):
    """The parsed body of a GET on `url`, re-sent after a 429 while `window` allows.

    Raises:
        core.Unreadable: curl failed.
        core.NonJsonResponse: the body is not JSON, a 429 that outlasted the retries included.
    """
    for delay in (*RETRY_DELAYS_S, None):
        status, body = get(url, resolve=resolve)
        if status != RATE_LIMITED or delay is None or not window.allows(delay):
            break
        sleep(delay)
    if status == 0:
        raise core.Unreadable(f"curl {url} failed: {body}")
    return core.parse_json(url, body)


def prom_query(promql, endpoint=None, fetch=None, get=None, sleep=None, window=None):
    """The `result` list of one instant query.

    Args:
        promql: the instant query.
        endpoint: returns `(base_url, resolve_pin)`; defaults to `core.prom_endpoint`.
        fetch: returns the parsed body of `(url, resolve=)`. When given, it replaces the
            status-aware read and its 429 retry.
        get: returns `(http_status, body)` for `(url, resolve=)`, status 0 for a failed curl;
            defaults to `core.get_status`.
        sleep: the backoff sleep; defaults to `time.sleep`.
        window: the `RetryWindow` a backoff sleep must end inside; defaults to
            `PROCESS_WINDOW`.

    Raises:
        core.Unreadable: curl failed, the edge kept answering 429, or Prometheus did not
            answer `success`.
    """
    base, pin = (endpoint or core.prom_endpoint)()
    url = prom_query_url(base, promql)
    if fetch is not None:
        data = fetch(url, resolve=pin)
    else:
        data = _get_json(
            url,
            pin,
            get or core.get_status,
            sleep or time.sleep,
            window or PROCESS_WINDOW,
        )
    if not isinstance(data, dict) or data.get("status") != "success":
        raise core.Unreadable(f"Prometheus refused {promql}: {str(data)[:300]}")
    return (data.get("data") or {}).get("result") or []


@dataclass(frozen=True)
class PromTools:
    """The boundaries `gate` crosses, so a test replaces a field rather than patching.

    `silences` reads Alertmanager's alerts for a tag. None skips that read, so a test that
    builds its own tools never reaches the network; `DEFAULT_PROM` sets the real one.
    `deadline` is the window every wait of `gate` must end inside. None bounds nothing;
    `DEFAULT_PROM` sets `PROCESS_DEADLINE`.
    """

    query: Callable[[str], list] = prom_query
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    silences: Callable[[str], list] | None = None
    deadline: RetryWindow | None = None


DEFAULT_PROM = PromTools(
    silences=alertmanager_api.service_alerts, deadline=PROCESS_DEADLINE
)


def _bounded(wait_s, tools):
    """`wait_s`, cut to what is left of `tools.deadline`."""
    if tools.deadline is None:
        return wait_s
    return min(wait_s, tools.deadline.remaining())


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


# The C6 label carrying the digest of a tag's own rendered directory, `cluster/<tag>/`. The
# renderer stamps it on Kustomization `<tag>` (bundle.py:CONTENT_DIGEST), so a master commit
# that leaves the tag unchanged keeps it while `revision` moves (#5338).
CONTENT_LABEL = "content_digest"


def flux_reference(tag, query):
    """When `tag`'s Kustomization first reported its current content, or None with no series.

    `gotk_resource_info` (C6) is one series per label set, so the earliest sample agreeing
    with the newest one on `content_digest` is the first report of the content now applied.
    A newest sample without the label, from a bundle or a kube-state-metrics older than it,
    falls back to `revision`: matching on an absent label would match every sample in the
    lookback.

    DECIDED: the earliest agreeing sample, not the start of the newest run — a revert to
    content seen earlier in the lookback reads back to that first report. That moves the
    reference earlier, which gates more alerts, never fewer, and keeps one instant query.
    Root `homelab` stamps the annotation and `<tag>` applies the content, unordered, so the
    reference can trail the content by one root apply plus one scrape.
    """
    selector = f'gotk_resource_info{{customresource_kind="Kustomization",name="{tag}"}}'
    newest = query(selector)
    if not newest:
        return None
    on = CONTENT_LABEL if _labels(newest[0]).get(CONTENT_LABEL) else "revision"
    rows = query(
        f"min(min_over_time(timestamp({selector})[{FLUX_LOOKBACK}:1m]) "
        f"and on({on}) {selector})"
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


PROBE_JOB = "blackbox-ingress"


def unprobed_route(tag, query):
    """(failing lines, note lines) for a routed `tag` with no `probe_success` series.

    The blackbox job's static labels reach its `up` series, so `up{service="<tag>"}` exists
    exactly when Prometheus has loaded the tag's target. With the target loaded, no probe
    series means the probe stopped reporting (blackbox-exporter down reads `up` 0), which
    fails. With the job loaded and the target absent, Flux has not applied the `observability`
    render that carries the route yet, so a new service's first gate would otherwise fail a
    healthy rollout (#5346). That passes with a note. With no `up` series for the job at all, the job is broken, which fails.

    `/api/v1/targets` would answer the same question, but Prometheus' IngressRoute admits
    `/api/v1/query` alone.
    """
    if query(f'up{{job="{PROBE_JOB}",service="{tag}"}}'):
        return [
            f"  {tag}'s probe target is loaded but reported no probe_success series "
            f"within {PROBE_WAIT_S}s"
        ], []
    if not query(f'up{{job="{PROBE_JOB}"}}'):
        return [
            f"  the {PROBE_JOB} job has no targets loaded, so {tag}'s route is unprobed"
        ], []
    return [], [
        f"  note: {tag} has an ingress route that Prometheus does not probe yet; Flux loads "
        f"its {PROBE_JOB} target when it applies `observability`, so re-run this gate then"
    ]


def judge_probes(tag, reference, tools):
    """(failing lines, note lines) for `tag`'s ingress probes, waiting for a fresh sample.

    `reference` is when the rollout completed. A sample at or before it may be the old pod's
    1 or a mid-rollout 0, so it decides nothing.
    """
    selector = f'probe_success{{service="{tag}"}}'
    deadline = tools.clock() + _bounded(PROBE_WAIT_S, tools)
    while True:
        values = {_key(s): s for s in tools.query(selector)}
        if not values:
            if tools.clock() >= deadline:
                return unprobed_route(tag, tools.query)
            tools.sleep(min(POLL_S, deadline - tools.clock()))
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
        tools.sleep(min(POLL_S, deadline - tools.clock()))
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
        tools: The Prometheus read, sleep and clock, and the Alertmanager read that names a
            gated alert's silence or inhibition without changing the exit code.

    Raises:
        core.Unreadable: a Prometheus read failed, or the re-read of a would-fail result
            would not end inside `tools.deadline`.
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
        if _bounded(REREAD_WAIT_S, tools) < REREAD_WAIT_S:
            raise core.Unreadable(
                f"{tag}: no time left for the {REREAD_WAIT_S}s re-read inside the "
                f"{PROBE_TIMEOUT_S}s probe timeout, so this read stays unconfirmed:"
                f"{failing[0]}"
            )
        tools.sleep(REREAD_WAIT_S)
        failing, notes = judge()
    if tools.silences is not None:
        notes += alertmanager_api.gate_notes(tag, tools.silences)
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

    The blackbox job loops over `containers_list | ingress_probe_targets`, so the same filter
    applied to this one entry is the render. An entry whose route was removed keeps an old
    `probe_success` series inside the lookback, which must not read as a stopped probe.
    """
    if entry is None:
        return False
    from renderer.template_filters.ingress_probes import ingress_probe_targets

    return bool(ingress_probe_targets([entry]))


def alert_reference(tag, query):
    """The time after which a firing alert of `tag` counts against this deploy (C7).

    When `tag`'s Kustomization first reported its current revision. None when that is
    unreadable or `tag` has no Kustomization, and then every gated alert fails.
    """
    return flux_reference(tag, query)


def check(tag, ready_at, entry, tools=DEFAULT_PROM):
    """`gate` for `tag`, with both reference times derived from its Kustomization and rollout.

    The probe reference is the later of the alert reference and the newest pod Ready
    transition: a sample before it may be the old pod's 1 or a mid-rollout 0.
    """
    reference = alert_reference(tag, tools.query)
    rolled = [t for t in (reference, ready_at) if t is not None]
    probe_reference = max(rolled) if rolled else tools.clock()
    return gate(tag, reference, probe_reference, has_ingress_route(entry), tools)
