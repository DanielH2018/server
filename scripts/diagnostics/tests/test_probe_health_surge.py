"""`probe.py health` refuses a Deployment whose old ReplicaSet still serves (#4563).

Under RollingUpdate with maxSurge >= 1 and maxUnavailable 0, a rollout runs an old pod and a
new pod at once. The old pod supplies readyReplicas and availableReplicas, the new pod supplies
updatedReplicas, and all three equal spec.replicas. Only status.replicas, which counts both
pods, shows the old ReplicaSet survives. The rest of the rollout verdict is in
`test_probe_health.py`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_surge.py
"""

from _probe_health_fixtures import NOW, pods

from diagnostics.probe_lib import health_rollout


def _surge_deploy(spec_replicas, replicas, updated, ready, available):
    """A Deployment whose `status.replicas` counts old and new pods together.

    `_deploy` leaves `status.replicas` out, which is the shape a finished rollout reads as. These
    tests need it, because a surviving old ReplicaSet shows up only there.
    """
    return {
        "metadata": {"generation": 13},
        "spec": {"replicas": spec_replicas},
        "status": {
            "observedGeneration": 13,
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
        },
    }


def test_k8s_health_old_replicaset_still_serving_exits_one():
    """The karakeep-chrome landing of #4551: the old pod is Ready, the new one is not (#4563).

    Under RollingUpdate with maxSurge 1 and maxUnavailable 0, the old pod supplies ready and
    available while the new pod supplies updated, so all three equal spec.replicas.
    `kubectl rollout status` refuses this state because status.replicas > updatedReplicas.
    """
    text, code = health_rollout.format_k8s_health(
        _surge_deploy(spec_replicas=1, replicas=2, updated=1, ready=1, available=1),
        pods(("app", 0, None)),
        "karakeep-chrome",
        NOW,
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_surge_fixture_pair_splits_on_status_replicas():
    """The red/green pair differs only in status.replicas, and the verdict follows it."""
    _, red = health_rollout.format_k8s_health(
        _surge_deploy(spec_replicas=1, replicas=2, updated=1, ready=1, available=1),
        pods(("app", 0, None)),
        "karakeep-chrome",
        NOW,
    )
    _, green = health_rollout.format_k8s_health(
        _surge_deploy(spec_replicas=1, replicas=1, updated=1, ready=1, available=1),
        pods(("app", 0, None)),
        "karakeep-chrome",
        NOW,
    )
    assert (red, green) == (1, 0)


def test_k8s_health_multi_replica_surge_with_an_old_pod_exits_one():
    """The rule is status.replicas == updatedReplicas, not a special case for one replica.

    Two desired, three running: two updated pods are counted, and the ready pair can still
    include the old one.
    """
    text, code = health_rollout.format_k8s_health(
        _surge_deploy(spec_replicas=2, replicas=3, updated=2, ready=2, available=2),
        pods(("app", 0, None)),
        "freshrss",
        NOW,
    )
    assert code == 1
    assert "rollout incomplete" in text
