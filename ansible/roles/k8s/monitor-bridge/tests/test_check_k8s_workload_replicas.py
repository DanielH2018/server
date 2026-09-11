"""check_k8s_workloads: the hysteresis on the unavailable-replica arm, and its boundaries.

k3s Workload Health opened 48 DOWN episodes over the 30 days to 2026-09-11, and the ones the
bridge's own log attributes to replicas are all one cycle long naming one rolling workload —
`unavailable replicas: uptime-kuma(1)` and five like it. A Deployment rolling has one
unavailable replica by definition, so that arm could not tell an ordinary rollout from a
workload that will not come back.

The gate is ARM-SELECTIVE, and that is what these tests have to prove. A blanket streak over the
whole check would pass a naive accept/reject pair while silently delaying every crash-loop page
by three cycles, so `test_a_crash_loop_pages_on_the_first_cycle_while_replicas_are_held` runs
both conditions at once and asserts the streak holds one and not the other.
"""

from dataclasses import replace
from functools import partial

import pytest
from checks.cluster import check_k8s_workloads

HEALTHY_COUNTS = {
    "count(kube_deployment_status_replicas_unavailable)": 40.0,
    "count(kube_daemonset_status_number_unavailable)": 9.0,
    "count(kube_node_status_allocatable)": 0.0,
}


@pytest.fixture
def kcfg(cfg):
    """A cluster Prometheus, no extended resources and no Loki arm — the replica arm alone."""
    return replace(
        cfg,
        CLUSTER_PROM_URL="http://cluster-prometheus:9090",
        K8S_EXTENDED_RESOURCES=(),
        LOG_ERROR_SELECTOR="",
    )


def _scalar(counts, _cfg, promql, **_kw):
    return counts.get(promql, 0.0)


def _vector(answers, _cfg, promql, **_kw):
    for fragment, vec in answers.items():
        if fragment in promql:
            return vec
    return []


@pytest.fixture
def vectors():
    """The dict a test fills with per-query answers, keyed by identifying PromQL substring.

    A test states only the arm it cares about; every other arm answers empty, which is healthy.
    """
    return {}


@pytest.fixture
def run(vectors):
    """Call the check through its injectable Prometheus boundaries."""

    def _run(kcfg, counts=HEALTHY_COUNTS):
        return check_k8s_workloads(
            kcfg, fetch=partial(_vector, vectors), scalar=partial(_scalar, counts)
        )

    return _run


ROLLING = [({"deployment": "radarr"}, 1.0)]
CRASH_LOOPING = [({"pod": "terraria-589575b44-9gj4g"}, 7.0)]


def test_every_workload_available_is_clean(kcfg, run):
    ok, msg = run(kcfg)
    assert ok, msg
    assert "40 k8s workloads healthy" in msg


def test_one_rolling_deployment_is_held_not_paged(kcfg, vectors, run):
    # The rollout case, and the one that produced the single-cycle episodes.
    vectors["kube_deployment_status_replicas_unavailable > 0"] = ROLLING
    ok, msg = run(kcfg)
    assert ok, msg
    assert "down streak 1/3 (rollout): unavailable replicas: radarr(1)" in msg


def test_the_third_straight_rolling_cycle_pages(kcfg, vectors, run):
    # The red proof: the gate delays, it does not suppress. A replica still unavailable after
    # K8S_WORKLOADS_CONSECUTIVE cycles is a workload that is not coming back.
    vectors["kube_deployment_status_replicas_unavailable > 0"] = ROLLING
    for _ in range(2):
        assert run(kcfg)[0]
    ok, msg = run(kcfg)
    assert not ok
    assert "k8s workloads with unavailable replicas: radarr(1)" in msg


def test_a_recovered_workload_resets_the_streak(kcfg, vectors, run):
    vectors["kube_deployment_status_replicas_unavailable > 0"] = ROLLING
    assert run(kcfg)[0]
    vectors["kube_deployment_status_replicas_unavailable > 0"] = []
    assert run(kcfg)[0]
    vectors["kube_deployment_status_replicas_unavailable > 0"] = ROLLING
    ok, msg = run(kcfg)
    assert ok, msg
    assert "down streak 1/3" in msg


def test_a_crash_loop_pages_on_the_first_cycle_while_replicas_are_held(
    kcfg, vectors, run
):
    # The arm-selectivity proof. Both conditions in one cycle: the replica arm is inside its
    # grace, the crash-loop arm is not gated at all, and the crash loop must still page.
    vectors["kube_deployment_status_replicas_unavailable > 0"] = ROLLING
    vectors["kube_pod_container_status_restarts_total"] = CRASH_LOOPING
    ok, msg = run(kcfg)
    assert not ok
    assert "crash-looping" in msg
    assert "terraria-589575b44-9gj4g(7)" in msg


def test_an_unavailable_daemonset_pages_on_the_first_cycle(kcfg, vectors, run):
    vectors["kube_daemonset_status_number_unavailable > 0"] = [
        ({"daemonset": "promtail"}, 1.0)
    ]
    ok, msg = run(kcfg)
    assert not ok
    assert "k8s daemonsets with unavailable pods: promtail(1)" in msg


def test_a_thin_deployment_census_pages_on_the_first_cycle(kcfg, run):
    # The floor arm reports the check is BLIND. Delaying that helps nobody, so it keeps no grace.
    ok, msg = run(kcfg, counts=dict.fromkeys(HEALTHY_COUNTS, 1.0))
    assert not ok
    assert "UNKNOWN, not OK" in msg
