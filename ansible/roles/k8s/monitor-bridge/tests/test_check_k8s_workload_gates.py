"""The two cluster-Prometheus checks' verdicts: k8s workloads and cluster scrape targets.

Both fail CLOSED on an absent series, which is the property worth pinning: `unavailable > 0`
returns an empty vector when everything is healthy AND when there are no series at all, so
reading the healthy meaning onto both is how a monitor goes green while blind. The floors
(`min_workloads`, `CLUSTER_TARGETS_MIN`) are what separate the two cases.

The GATE above these — cluster Prometheus unreachable — is `test_check_gates.py`; membership of
`CLUSTER_DEPENDENT` is `test_check_gate_dependents.py`.
"""

from dataclasses import replace

import bridge.net
import checks.cluster


def test_k8s_workloads_absent_series_is_down_not_up():
    # THE regression this check exists to prevent. `unavailable > 0` returns an empty vector both
    # when everything is healthy and when there are no series at all; reading the healthy meaning
    # onto both is how a monitor goes green while blind.
    ok, msg = checks.cluster.k8s_workloads_verdict(None, [], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_workloads_partial_series_is_down():
    # A partially-loaded kube-state-metrics — e.g. `apps` dropped from its scoped ClusterRole,
    # which takes every deployment series away while the pod stays up and Ready.
    ok, msg = checks.cluster.k8s_workloads_verdict(2, [], 5)
    assert ok is False
    assert "below the floor" in msg


def test_k8s_workloads_healthy_when_series_present_and_none_unavailable():
    ok, msg = checks.cluster.k8s_workloads_verdict(18, [], 5)
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_k8s_workloads_names_the_offenders():
    offenders = [
        ({"deployment": "n8n-runners"}, 1.0),
        ({"deployment": "registry"}, 2.0),
    ]
    ok, msg = checks.cluster.k8s_workloads_verdict(18, offenders, 5)
    assert ok is False
    # Sorted, so the message is stable rather than dependent on Prometheus' series order.
    assert "n8n-runners(1), registry(2)" in msg


def test_k8s_workloads_crash_loop_is_down_despite_available_replicas():
    # The 2026-08-13 homepage incident: a CrashLoopBackOff pod passes readiness for a brief
    # window each backoff cycle, so replica availability read healthy through 31 restarts.
    # The restart counter is the signal that doesn't flap.
    restarts = [({"pod": "homepage-58d867556f-7qbz9"}, 6.0)]
    ok, msg = checks.cluster.k8s_workloads_verdict(18, [], 5, restarts)
    assert ok is False
    assert "crash-looping" in msg
    assert "homepage-58d867556f-7qbz9(6)" in msg


def test_k8s_workloads_unavailable_replicas_outrank_the_restart_arm():
    # Both arms firing is one incident; the replica message is the more actionable one.
    offenders = [({"deployment": "homepage"}, 1.0)]
    restarts = [({"pod": "homepage-x"}, 6.0)]
    ok, msg = checks.cluster.k8s_workloads_verdict(18, offenders, 5, restarts)
    assert ok is False
    assert "unavailable replicas" in msg


def test_k8s_daemonsets_absent_series_is_down_not_up():
    # Same fail-closed shape as the deployment arm: an absent DaemonSet series is UNKNOWN,
    # not "no DaemonSets have a problem".
    ok, msg = checks.cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=None, min_daemonsets=9
    )
    assert ok is False
    assert "UNKNOWN" in msg


def test_k8s_daemonsets_partial_series_is_down():
    ok, msg = checks.cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=3, min_daemonsets=9
    )
    assert ok is False
    assert "below the floor" in msg


def test_k8s_daemonsets_names_the_offenders():
    ds_offenders = [({"daemonset": "otel-collector"}, 1.0)]
    ok, msg = checks.cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=9, ds_offenders=ds_offenders, min_daemonsets=9
    )
    assert ok is False
    assert "otel-collector(1)" in msg


def test_k8s_daemonsets_healthy_alongside_healthy_deployments():
    ok, msg = checks.cluster.k8s_workloads_verdict(
        18, [], 5, ds_total=9, min_daemonsets=9
    )
    assert ok is True
    assert "18 k8s workloads healthy" == msg


def test_k8s_workloads_disabled_without_cluster_url(monkeypatch, cfg):
    cfg = replace(cfg, CLUSTER_PROM_URL="")
    ok, msg = checks.cluster.check_k8s_workloads(cfg)
    assert ok is True
    assert "disabled" in msg


def test_cluster_targets_covers_everything_its_sibling_does_not(monkeypatch, cfg):
    """`origin!="daniel-server"` is the complement of check_targets_down's pin, so every `up`
    series belongs to exactly one of the two checks.

    THE GAP THIS PINS (2026-08-15): the previous `origin=""` matched only series where the label
    is ABSENT (cluster-native). daniel-box's node-exporter carries `origin="daniel-box"`, so it
    matched NEITHER check and could have died watched by nothing.
    """
    seen = {}

    def fake_vector(_cfg, promql, base=None, source="prometheus"):
        seen["q"], seen["base"] = promql, base
        return [({"job": "j%d" % i}, 1.0) for i in range(5)]

    cfg = replace(cfg, CLUSTER_PROM_URL="https://cluster")
    monkeypatch.setattr(bridge.net, "prom_vector", fake_vector)
    ok, _ = checks.cluster.check_cluster_targets(cfg)
    assert ok is True
    assert seen["q"] == 'up{origin!="daniel-server"}'
    assert seen["base"] == "https://cluster"


def test_cluster_targets_empty_is_down(cfg):
    ok, msg = checks.cluster.targets_verdict([], cfg.CLUSTER_TARGETS_MIN)
    assert ok is False
    assert "UNKNOWN" in msg


def test_cluster_targets_disabled_without_cluster_url(monkeypatch, cfg):
    cfg = replace(cfg, CLUSTER_PROM_URL="")
    ok, msg = checks.cluster.check_cluster_targets(cfg)
    assert ok is True
    assert "disabled" in msg


def test_targets_empty_vector_is_down_not_all_clear():
    # THE hole B5 opens. Before the repoint an empty `up` could only mean the queried Prometheus
    # was down, and the PROM_DEPENDENT gate suppressed this check first. Against the cluster copy
    # the gate passes (that Prometheus is fine) while `up{origin="daniel-server"}` is empty, and
    # the old code returned "all 0 targets up".
    ok, msg = checks.cluster.targets_verdict([], 5)
    assert ok is False
    assert "UNKNOWN" in msg


def test_targets_below_floor_is_down():
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    ok, msg = checks.cluster.targets_verdict(vec, 5)
    assert ok is False
    assert "below the floor" in msg


def test_targets_names_down_jobs_above_the_floor():
    vec = [({"job": "node"}, 0.0)] + [({"job": "j%d" % i}, 1.0) for i in range(5)]
    ok, msg = checks.cluster.targets_verdict(vec, 5)
    assert ok is False
    assert "1 target(s) down: node" in msg


def test_targets_all_up_above_the_floor():
    vec = [({"job": "j%d" % i}, 1.0) for i in range(11)]
    ok, msg = checks.cluster.targets_verdict(vec, 5)
    assert ok is True
    assert msg == "all 11 targets up"
