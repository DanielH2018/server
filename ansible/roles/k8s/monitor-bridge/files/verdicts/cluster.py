"""Cluster-health verdicts for check.py — what the k3s numbers mean, not how they are read.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge/parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

`log_error_verdict`'s INERT arm is why this logic is worth isolating: a selector that matches no
stream reads as a clean result unless a volume count says otherwise.
"""

from collections.abc import Collection


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
