"""Cluster-health verdicts for check.py — what the k3s numbers mean, not how they are read.

These decide; check.py fetches. Each takes its inputs as arguments and reads no module-level
config, which is what makes it safe to live here — see bridge_parsing.py's header for the rule
and why breaking it fails silently rather than loudly.

`k8s_workloads_verdict`'s fail-closed reasoning is why this logic is worth isolating:
`unavailable > 0` returns an empty vector both when every workload is healthy AND when there
are no series at all, so the count floors are what stop a blind check reading green.
"""


def targets_verdict(vec, min_targets):
    """Pure: (ok, msg) from an `up` vector, failing closed when too few targets are visible.

    THE HOLE THIS CLOSES, opened by B5. Before the repoint an empty `up` could only mean the
    Prometheus being queried was down, and the PROM_DEPENDENT gate suppressed this check before it
    ran. Pointed at the cluster copy those two facts come apart: the gate probes the CLUSTER, which
    is up and answering, while `up{origin="daniel-server"}` goes empty the moment daniel-server's
    Prometheus stops remote-writing. `len(down) == 0` is then trivially true and this reports
    "all 0 targets up" — green, and blind to an entire estate having vanished.

    Same fail-closed shape as the k8s workload floor: count first, and treat "fewer series than
    could possibly be right" as UNKNOWN rather than healthy. This check is also the sentinel for
    the other estate-pinned checks — restarts/oom/cpu legitimately return empty when nothing is
    wrong, so they cannot tell "quiet" from "gone", and this one can.
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


def ksm_resource_label(resource):
    """Turn a Kubernetes resource name into the `resource` label kube-state-metrics emits.

    KSM replaces every character outside [a-zA-Z0-9_] with `_`, so `devic_es_dri` is the only form
    that matches a series — while `devic.es/dri` is what `kubectl describe node` prints and what an
    operator would configure. Querying the unsanitised name matches nothing, and this check reads
    "matches nothing" as the device plugin having deregistered. That is a DOWN on a healthy
    cluster, which is what it did live from 18:05 to 18:35 UTC on 2026-08-20.
    """
    return "".join(c if c.isalnum() or c == "_" else "_" for c in resource)


def extended_resource_verdict(expected, advertised, allocatable_series):
    """Pure: (ok, msg) for extended resources that must stay advertised by some node.

    `advertised` maps resource name -> number of nodes advertising a NON-ZERO quantity.
    `allocatable_series` is the total count of kube_node_status_allocatable series, and it is what
    separates the two ways this can come back empty:

      - no series at all  -> kube-state-metrics is not collecting `nodes`, so the question was
        never asked. Reported as INERT rather than as a fault, and named in the message: a check
        that cannot read its input must not answer as though it did, in either direction. Silently
        passing would be the failure this arm exists to fix; silently failing would page for a
        collector change nobody made.
      - series exist, resource absent -> the resource is genuinely gone. That is the fault.

    A resource advertised by zero nodes is identical to one that never appears, and both are a
    fault once the collector is confirmed running: the pods that need it cannot schedule either way.
    """
    if not expected:
        return True, "no extended resources watched"
    if not allocatable_series:
        return True, (
            "extended-resource check INERT: no kube_node_status_allocatable series "
            "(kube-state-metrics is not collecting `nodes`); %s unwatched"
            % ", ".join(expected)
        )
    missing = [r for r in expected if not advertised.get(r)]
    if missing:
        return False, (
            "extended resource(s) advertised by no node: %s — the device plugin is Running but "
            "its resource is deregistered; pods requesting it cannot schedule"
            % ", ".join(
                "%s (kube-state-metrics label %s)" % (r, ksm_resource_label(r))
                for r in missing
            )
        )
    return True, "extended resource(s) advertised: %s" % ", ".join(
        "%s on %d node(s)" % (r, advertised.get(r, 0)) for r in expected
    )


def k8s_workloads_verdict(
    total,
    offenders,
    min_workloads,
    restart_offenders=(),
    ds_total=None,
    ds_offenders=(),
    min_daemonsets=None,
):
    """Pure: (ok, msg) from the deployment-series COUNT and the unavailable-replica offenders.

    The count argument is what makes this fail closed. `unavailable > 0` returning nothing is
    ambiguous — it means either "every workload is healthy" or "there are no series at all" —
    and only the second is a fault. Reading the first interpretation onto both is how a monitor
    goes green while blind, so the count is checked BEFORE the offender list is trusted.

    restart_offenders is the crash-loop arm (2026-08-13): a CrashLoopBackOff pod passes its
    readiness probe for a brief window each backoff cycle, so replica availability AND a 60s
    HTTP tile both mostly read healthy — homepage crash-looped 31 times overnight with this
    check green. A restart counter that climbed past the threshold is down regardless of what
    readiness says right now.

    ds_total/ds_offenders/min_daemonsets are the DaemonSet arm (2026-08-13): a DaemonSet has no
    Deployment-arm equivalent, so an absent or unschedulable DS pod (a node NotReady, a
    node-selector mismatch, a node lacking a required resource) was invisible. Same fail-closed
    shape as the deployment arm; min_daemonsets left None means the caller didn't supply
    DaemonSet data (existing callers/tests), so this arm is skipped rather than treated as zero
    DaemonSets.
    """
    if total is None:
        return False, (
            "kube_deployment_status_replicas_unavailable is absent from the cluster Prometheus "
            "— kube-state-metrics is not being scraped, so workload health is UNKNOWN, not OK"
        )
    if total < min_workloads:
        return False, (
            "only %d deployment series in the cluster Prometheus, below the floor of %d — "
            "kube-state-metrics is partially loaded, so workload health is UNKNOWN, not OK"
            % (int(total), min_workloads)
        )
    if min_daemonsets is not None:
        if ds_total is None:
            return False, (
                "kube_daemonset_status_number_unavailable is absent from the cluster Prometheus "
                "— kube-state-metrics is not being scraped, so daemonset health is UNKNOWN, not OK"
            )
        if ds_total < min_daemonsets:
            return False, (
                "only %d daemonset series in the cluster Prometheus, below the floor of %d — "
                "kube-state-metrics is partially loaded, so daemonset health is UNKNOWN, not OK"
                % (int(ds_total), min_daemonsets)
            )
    if offenders:
        named = ", ".join(
            "%s(%d)" % (labels.get("deployment", "?"), int(value))
            for labels, value in sorted(
                offenders, key=lambda o: o[0].get("deployment", "")
            )
        )
        return False, "k8s workloads with unavailable replicas: %s" % named
    if ds_offenders:
        named = ", ".join(
            "%s(%d)" % (labels.get("daemonset", "?"), int(value))
            for labels, value in sorted(
                ds_offenders, key=lambda o: o[0].get("daemonset", "")
            )
        )
        return False, "k8s daemonsets with unavailable pods: %s" % named
    if restart_offenders:
        named = ", ".join(
            "%s(%d)" % (labels.get("pod", "?"), int(value))
            for labels, value in sorted(
                restart_offenders, key=lambda o: o[0].get("pod", "")
            )
        )
        return False, "k8s pods crash-looping (restarts in window): %s" % named
    return True, "%d k8s workloads healthy" % int(total)
