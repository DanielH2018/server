"""Cluster-health verdicts for check.py — what the k3s numbers mean, not how they are read.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge/parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

`cadvisor_coverage_shortfall`'s fail-closed reasoning is why this logic is worth isolating: a
query that matches no pod reads as a clean result unless a coverage floor says otherwise.
"""

from collections.abc import Collection


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
