"""check_k8s_workloads: the zero-available arm and its shorter grace (#1802).

A Deployment with no available replicas is down, not rolling, and the unavailable-replica arm's
K8S_WORKLOADS_CONSECUTIVE grace held one (authelia, 2026-09-10, 11 minutes at zero) as if it
were mid-rollout. This arm reads the same Deployments through `available == 0` and pages at
K8S_ZERO_AVAILABLE_CONSECUTIVE, which is shorter. The proofs: a Recreate swap (one cycle at
zero) is held, the second straight cycle pages, the two streaks are independent, and a
Deployment scaled to zero on purpose is not an offender — the query excludes it, and the test
states that arm's answer the way the query would.
"""

from dataclasses import replace
from functools import partial

import pytest
import bridge.streaks
from checks.cluster import check_k8s_workloads

HEALTHY_COUNTS = {
    "count(kube_deployment_status_replicas_unavailable)": 40.0,
    "count(kube_daemonset_status_number_unavailable)": 9.0,
    "count(kube_node_status_allocatable)": 0.0,
}

ZERO_QUERY = "kube_deployment_status_replicas_available == 0"
DESIRED_QUERY = "kube_deployment_spec_replicas"
UNAVAILABLE_QUERY = "kube_deployment_status_replicas_unavailable > 0"
STALL_QUERY = "kube_deployment_status_replicas_updated"

AUTHELIA_AT_ZERO = [({"namespace": "homelab", "deployment": "authelia"}, 0.0)]
AUTHELIA_DESIRED = [({"namespace": "homelab", "deployment": "authelia"}, 1.0)]
AUTHELIA_UNAVAILABLE = [({"deployment": "authelia"}, 1.0)]


@pytest.fixture
def kcfg(cfg):
    return replace(
        cfg,
        CLUSTER_PROM_URL="http://cluster-prometheus:9090",
        K8S_EXTENDED_RESOURCES=(),
        LOG_ERROR_SELECTOR="",
        K8S_WORKLOADS_CONSECUTIVE=3,
        K8S_ZERO_AVAILABLE_CONSECUTIVE=2,
    )


def _scalar(counts, _cfg, promql, **_kw):
    return counts.get(promql, 0.0)


def _vector(answers, _cfg, promql, **_kw):
    # Longest fragment first: the zero query joins on DESIRED_QUERY's series name.
    for fragment in sorted(answers, key=len, reverse=True):
        if fragment in promql:
            return answers[fragment]
    return []


@pytest.fixture(autouse=True)
def _clear_streaks():
    bridge.streaks._down_streaks.clear()
    yield
    bridge.streaks._down_streaks.clear()


@pytest.fixture
def run():
    def _run(kcfg, vectors):
        return check_k8s_workloads(
            kcfg,
            fetch=partial(_vector, vectors),
            scalar=partial(_scalar, HEALTHY_COUNTS),
        )

    return _run


def _at_zero():
    # What kube-state-metrics says of a Deployment at zero: the unavailable arm sees it too.
    # The stall query joins on DESIRED_QUERY's series name too, so it is named empty here.
    return {
        ZERO_QUERY: AUTHELIA_AT_ZERO,
        DESIRED_QUERY: AUTHELIA_DESIRED,
        UNAVAILABLE_QUERY: AUTHELIA_UNAVAILABLE,
        STALL_QUERY: [],
    }


def test_one_cycle_at_zero_is_a_recreate_swap_and_is_held(kcfg, run):
    ok, msg = run(kcfg, _at_zero())
    assert ok, msg
    assert "down streak 1/2 (cold start): no available replicas: authelia(0/1)" in msg
    assert "down streak 1/3 (rollout): unavailable replicas: authelia(1)" in msg


def test_the_second_straight_cycle_at_zero_pages_before_the_replica_arm_would(
    kcfg, run
):
    # The red proof, and the #1802 verify-by: a Deployment at zero pages within two cycles,
    # while the unavailable-replica arm holding the same workload is still one cycle short.
    assert run(kcfg, _at_zero())[0]
    ok, msg = run(kcfg, _at_zero())
    assert not ok
    assert (
        "k8s workloads with NO available replicas (available/desired): authelia(0/1)"
        in msg
    )
    assert "down streak 2/3 (rollout)" in msg


def test_a_partial_outage_does_not_advance_the_zero_streak(kcfg, run):
    # One of two replicas down is the unavailable arm's case alone; the zero streak must not
    # accumulate on it, or a partial outage followed by a swap pages on the swap.
    partial_outage = {UNAVAILABLE_QUERY: [({"deployment": "grafana"}, 1.0)]}
    assert run(kcfg, partial_outage)[0]
    ok, msg = run(kcfg, _at_zero())
    assert ok, msg
    assert "down streak 1/2 (cold start)" in msg
    assert "down streak 2/3 (rollout)" in msg


def test_a_recovered_deployment_resets_the_zero_streak(kcfg, run):
    assert run(kcfg, _at_zero())[0]
    assert run(kcfg, {})[0]
    ok, msg = run(kcfg, _at_zero())
    assert ok, msg
    assert "down streak 1/2 (cold start)" in msg


def test_a_deployment_scaled_to_zero_on_purpose_is_not_an_offender(kcfg, run):
    # The query's `and ... spec_replicas > 0` drops it, so the arm's answer is empty; the
    # unavailable arm has nothing either, because a scaled-down Deployment wants nothing.
    ok, msg = run(kcfg, {ZERO_QUERY: [], DESIRED_QUERY: []})
    assert ok, msg
    assert "40 k8s workloads healthy" in msg
    assert "cold start" not in msg
