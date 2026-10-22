"""Cluster-health verdicts for check.py — what the k3s numbers mean, not how they are read.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge/parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

`cadvisor_coverage_shortfall`'s fail-closed reasoning is why this logic is worth isolating: a
query that matches no pod reads as a clean result unless a coverage floor says otherwise.
"""

from collections.abc import Collection, Mapping


def targets_verdict(
    vec: list[tuple[dict, float]], min_targets: float
) -> tuple[bool, str]:
    """Pure: (ok, msg) from an `up` vector, failing closed when too few targets are visible.

    THE HOLE THIS CLOSES, opened by B5. Before the repoint an empty `up` could only mean the
    Prometheus being queried was down, and the PROM_DEPENDENT gate suppressed this check before it
    ran. Pointed at the cluster copy those two facts come apart: the gate probes the CLUSTER, which
    is up and answering, while `up{origin="daniel-server"}` goes empty the moment daniel-server's
    Prometheus stops remote-writing. `len(down) == 0` is then trivially true and this reports
    "all 0 targets up" — green, and blind to an entire estate having vanished.

    Fail-closed shape: count first, and treat "fewer series than could possibly be right" as
    UNKNOWN rather than healthy.

    This is NOT a sentinel for restarts/oom/cpu, though it claimed to be until 2026-08-27. It
    watches `up`, and `up` was entirely healthy from the Phase G retarget to 2026-08-24 while an
    origin-pinned selector emptied all three cAdvisor checks — the one occurrence this coverage
    argument was supposed to cover, missed. Those three carry their own floor now; see
    `cadvisor_coverage_shortfall`.
    """
    if len(vec) < min_targets:
        return False, (
            "only %d scrape targets visible, below the floor of %d — the metrics estate is "
            "missing, so target health is UNKNOWN, not OK" % (len(vec), min_targets)
        )
    down = sorted({m.get("job") or m.get("instance") or "?" for m, v in vec if v == 0})
    if down:
        return False, "%d target(s) down: %s" % (len(down), ", ".join(down))
    return True, "all %d targets up" % len(vec)


def cadvisor_coverage_shortfall(
    pod_count: float, min_pods: float, what: str
) -> str | None:
    """Pure: the failure message when a cAdvisor vector is too thin to mean anything, else None.

    check_restarts, check_oom and check_cpu_throttle all read a per-pod vector and then filter it
    to offenders. Empty-after-filtering is the healthy answer, so none of the three can tell
    "nothing is wrong" from "the query matched nothing" — the exact split that let all three log
    OK off empty vectors from the Phase G retarget until 2026-08-24, leaving OOM kills and
    sustained CFS throttling with no alert path at all. It was found by reading the code, not by
    an alert.

    The vector counted here is the one the check already fetched, BEFORE the offender filter.
    `changes()`, `increase()` and `rate()` all return 0 for a quiet series rather than dropping it,
    so the pre-filter length is a pod count and a real coverage signal. That is why this needs no
    query of its own — a separate probe could disagree with the vector the verdict is built on.

    # DECIDED: floor 20 pods, from measurement rather than intuition. Over the 7d to 2026-08-27
    # the pre-filter counts never fell below 98 (restarts), 98 (oom) and 70 (cpu, which sees only
    # pods carrying a cpu limit); live values were 99/99/71. Count the DIVISION for the cpu arm,
    # not its denominator: check_cpu_throttle floors `ratio_vec`, and a PromQL binary op drops
    # series present on only one side, so the two can differ. They happened to agree here — both
    # 71 live, 70 at the trough — which is exactly why the easier query is the wrong one to
    # re-measure from. 20 sits 3.5x under the smallest
    # observed trough, so a deploy draining pods cannot reach it, while a broken selector or a
    # vanished kubernetes-cadvisor job lands at 0. One floor for all three, not one each: a floor
    # applied to two of three would reproduce the selector-drift class inside its own fix.
    """
    if pod_count >= min_pods:
        return None
    return (
        "%s UNKNOWN: only %d pod(s) visible to cAdvisor, below the floor of %d — the query is "
        "matching nothing, so this is not a clean result" % (what, pod_count, min_pods)
    )


def log_error_verdict(
    matches: list[tuple[dict, float]],
    total: float | None,
    threshold: float,
    window: str,
    ignore: Collection[str] = frozenset(),
) -> tuple[bool, str]:
    """Per-container counts of fatal log lines -> a verdict. Pure.

    The Workload Log Errors row's verdict. It answers the question Kubernetes state structurally
    cannot: replicas, restarts and allocatable all report a container that is Ready while failing
    at its actual job as healthy, because by their measure it is. Readiness asks whether the port
    is open.

    `total` is the selector's own volume, and it is what keeps this honest. The arm fails OPEN
    (see checks/logs.py's with_log_errors), so a selector matching no stream returns no matches and
    reads exactly like a healthy estate — the trap that shipped HA_BAN_SELECTOR with an `app`
    label the log shipper does not emit and reported "no ip_ban events" through a window containing a
    real ban. Counting the volume separates "nothing is wrong" from "I asked the wrong
    question", so a zero here reports INERT rather than OK.
    """
    if not total:
        return True, "log-error arm INERT (selector matched no lines)"
    offenders = [
        (labels.get("container", "?"), count)
        for labels, count in matches
        if count > threshold and labels.get("container", "").lower() not in ignore
    ]
    if not offenders:
        return True, "no log-error bursts in %s" % window
    offenders.sort(key=lambda nc: -nc[1])
    named = ", ".join("%s (%d)" % (name, count) for name, count in offenders[:5])
    return False, "fatal log lines in %s: %s" % (window, named)


def traefik_latency_verdict(
    total: Mapping[str, float],
    under: Mapping[str, float],
    min_rps: float,
    slow_pct: float,
    slow_bucket: str,
    min_slow_requests: float,
    stream_prefixes: Collection[str],
    window_s: float,
) -> tuple[bool, str]:
    """Pure: (ok, msg) from a service's request rate and its rate under the slow bucket.

    Three ways a service leaves the offender list, and they are not interchangeable. Below
    `min_rps` it is too quiet to judge. Matching `stream_prefixes` it is not measurable at all,
    because Traefik times a request until the response completes and a long-lived connection
    therefore sits past every bucket edge while healthy. Missing from `under` its `le=` selected
    nothing, which is a broken bucket configuration rather than a fast service — reading that as
    "0 requests under the boundary" would page every service at once.

    An offender must clear BOTH the ratio and `min_slow_requests`. `min_rps` admits a service
    with `min_rps * window_s` requests, so at the deployed values one slow request already
    exceeds `slow_pct`; the count is what makes the percentage mean something.

    Args:
      total: per-service request rate, from the histogram's own `_count` series.
      under: per-service rate under `slow_bucket`, from the same metric family so numerator and
        denominator are always the same scrape.
      window_s: the range the two rates were taken over, used to turn a rate into a count.

    Returns:
      (ok, msg). The green message names how many services were exempt, because an exempt
      service is not measured and a bare "ok" would overstate the coverage.
    """
    offenders = []
    unmeasurable = []
    eligible = 0
    exempt = 0
    worst = 0.0
    for svc, rps in total.items():
        if rps < min_rps:
            continue
        if any(svc.startswith(p) for p in stream_prefixes):
            exempt += 1
            continue
        if svc not in under:
            unmeasurable.append(svc)
            continue
        eligible += 1
        pct = 100.0 * (1.0 - under[svc] / rps)
        worst = max(worst, pct)
        if pct > slow_pct and (rps - under[svc]) * window_s >= min_slow_requests:
            offenders.append((svc, pct, rps))
    if unmeasurable:
        return False, (
            "no %ss bucket for %d service(s) (%s) — check Traefik's histogram buckets"
            % (slow_bucket, len(unmeasurable), ", ".join(sorted(unmeasurable)[:5]))
        )
    offenders.sort(key=lambda spr: -spr[1])
    if offenders:
        desc = ", ".join("%s (%.0f%% of %.2f rps)" % o for o in offenders[:5])
        return False, (
            "%d service(s) with over %.0f%% of requests slower than %ss: %s"
            % (len(offenders), slow_pct, slow_bucket, desc)
        )
    return True, (
        "latency ok: %d service(s) above floor, worst %.1f%% over %ss%s"
        % (
            eligible,
            worst,
            slow_bucket,
            ", %d stream service(s) exempt" % exempt if exempt else "",
        )
    )
