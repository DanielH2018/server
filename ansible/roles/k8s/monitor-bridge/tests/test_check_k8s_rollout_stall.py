"""check_k8s_workloads: the stalled-rollout arm, and the two ways it could be green while blind.

A Deployment whose new ReplicaSet never gets a pod passes every other arm of this check. The old
pod stays Ready, so `kube_deployment_status_replicas_unavailable` is 0; nothing restarts, so the
crash-loop arm is quiet; the series counts are unchanged, so the floors hold. Only
`kube_deployment_status_replicas_updated` moves.

This is the complement of the 2026-09-10 Authelia stall (#1783), not a second reading of it —
kube-state-metrics has authelia at updated=1, available=0, unavailable=1 for that window, which
the unavailable-replica arm sees.

Every test here comes as an accept/reject pair, because the arm has two failure shapes that both
read green: never firing (the query stops matching, e.g. kube-state-metrics renames a series) and
always firing (the streak gate stops holding, so ordinary rollouts page).
"""

from dataclasses import replace
from functools import partial

import bridge.streaks
import pytest
from checks.cluster import check_k8s_workloads

HEALTHY_COUNTS = {
    "count(kube_deployment_status_replicas_unavailable)": 40.0,
    "count(kube_daemonset_status_number_unavailable)": 9.0,
    "count(kube_node_status_allocatable)": 0.0,
}

STALL_QUERY = "kube_deployment_status_replicas_updated"
DESIRED_QUERY = "kube_deployment_spec_replicas"

AUTHELIA_STALLED = [({"namespace": "homelab", "deployment": "authelia"}, 0.0)]
AUTHELIA_DESIRED = [({"namespace": "homelab", "deployment": "authelia"}, 1.0)]


@pytest.fixture
def kcfg(cfg):
    """A cluster Prometheus, no extended resources and no Loki arm — the rollout arm alone."""
    return replace(
        cfg,
        CLUSTER_PROM_URL="http://cluster-prometheus:9090",
        K8S_EXTENDED_RESOURCES=(),
        LOG_ERROR_SELECTOR="",
        K8S_ROLLOUT_STALL_CONSECUTIVE=3,
    )


def _scalar(counts, _cfg, promql, **_kw):
    return counts.get(promql, 0.0)


def _vector(answers, _cfg, promql, **_kw):
    # Longest fragment first: "kube_deployment_spec_replicas" is a substring of the stall query,
    # so a shortest-first match would answer the stall query with the desired vector.
    for fragment in sorted(answers, key=len, reverse=True):
        if fragment in promql:
            return answers[fragment]
    return []


@pytest.fixture
def vectors():
    """Per-query answers keyed by an identifying PromQL substring; unnamed arms answer empty."""
    return {}


@pytest.fixture(autouse=True)
def _clear_streaks():
    bridge.streaks._down_streaks.clear()
    yield
    bridge.streaks._down_streaks.clear()


@pytest.fixture
def run(vectors):
    def _run(kcfg, counts=HEALTHY_COUNTS):
        return check_k8s_workloads(
            kcfg, fetch=partial(_vector, vectors), scalar=partial(_scalar, counts)
        )

    return _run


def test_a_cluster_with_no_rollout_in_flight_is_clean(kcfg, run):
    ok, msg = run(kcfg)
    assert ok
    assert "rollout" not in msg


def test_a_rollout_stuck_past_the_streak_is_flagged(kcfg, vectors, run):
    vectors[STALL_QUERY] = AUTHELIA_STALLED
    vectors[DESIRED_QUERY] = AUTHELIA_DESIRED
    for _ in range(kcfg.K8S_ROLLOUT_STALL_CONSECUTIVE - 1):
        assert run(kcfg)[0]
    ok, msg = run(kcfg)
    assert not ok
    assert "rollout stalled" in msg
    assert "authelia(0/1)" in msg


def test_an_ordinary_rollout_that_completes_inside_the_streak_is_clean(
    kcfg, vectors, run
):
    """The reject half of the gate: a rollout in flight for two cycles must not page.

    Without this, raising the arm's sensitivity to catch a stall faster would be invisible here
    — the flagged test alone passes whether the streak is 3 or 0.
    """
    vectors[STALL_QUERY] = AUTHELIA_STALLED
    vectors[DESIRED_QUERY] = AUTHELIA_DESIRED
    for _ in range(kcfg.K8S_ROLLOUT_STALL_CONSECUTIVE - 1):
        ok, msg = run(kcfg)
        assert ok
        assert "down streak" in msg
        assert "authelia(0/1)" in msg
    vectors[STALL_QUERY] = []
    ok, msg = run(kcfg)
    assert ok
    assert "rollout" not in msg


def test_the_streak_resets_so_two_separate_rollouts_do_not_add_up(kcfg, vectors, run):
    vectors[STALL_QUERY] = AUTHELIA_STALLED
    vectors[DESIRED_QUERY] = AUTHELIA_DESIRED
    assert run(kcfg)[0]
    vectors[STALL_QUERY] = []
    assert run(kcfg)[0]
    vectors[STALL_QUERY] = AUTHELIA_STALLED
    for _ in range(kcfg.K8S_ROLLOUT_STALL_CONSECUTIVE - 1):
        assert run(kcfg)[0], "the streak did not reset when the rollout finished"
    assert not run(kcfg)[0]


def test_an_unavailable_replica_still_outranks_a_stalled_rollout(kcfg, vectors, run):
    """Arm order, asserted rather than assumed: a pod that is DOWN beats a pod serving old config.

    Both arms can hold at once — a rollout that half-lands leaves one ReplicaSet short and one
    replica unavailable — and the message names one of them.
    """
    vectors[STALL_QUERY] = AUTHELIA_STALLED
    vectors[DESIRED_QUERY] = AUTHELIA_DESIRED
    vectors["kube_deployment_status_replicas_unavailable >"] = [
        ({"deployment": "grafana"}, 1.0)
    ]
    for _ in range(max(kcfg.K8S_ROLLOUT_STALL_CONSECUTIVE, 3)):
        ok, msg = run(kcfg)
    assert not ok
    assert "unavailable replicas: grafana(1)" in msg


def test_a_deployment_whose_desired_count_is_missing_still_names_the_workload(
    kcfg, vectors, run
):
    """The desired vector is a second query, so it can come back without the stalled series."""
    vectors[STALL_QUERY] = AUTHELIA_STALLED
    vectors[DESIRED_QUERY] = []
    for _ in range(kcfg.K8S_ROLLOUT_STALL_CONSECUTIVE):
        ok, msg = run(kcfg)
    assert not ok
    assert "authelia(0/?)" in msg
