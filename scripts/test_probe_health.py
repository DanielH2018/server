"""`probe.py health <svc>`: the post-deploy gate.

It exits 0 only when the workload is fully rolled out AND nothing restarted in the last 180s.
Both halves matter: readiness flips a Deployment to Available before a bad liveness probe starts
killing it, so a rollout check alone reports green on a crashlooping pod. An unreadable restart
time counts as recent, so the gate fails closed.
"""

import importlib.util
import os
from datetime import datetime, timezone


_MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
_spec = importlib.util.spec_from_file_location("probe", _MOD)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


# Fake resolver: maps container name -> a recognizable IP. A wrong container name
# raises KeyError, so a misrouted subcommand fails loudly.
IPS = {"prometheus": "10.0.0.1", "loki": "10.0.0.2", "scrutiny": "10.0.0.3"}
fake_resolve = IPS.__getitem__


def fake_k8s_endpoint(hostname):
    # The (base, --resolve pin) pair the live k8s_endpoint() derives from SOPS +
    # inventory — faked so plan() stays testable without either.
    return f"https://{hostname}.example", f"{hostname}.example:443:10.0.0.240"


def _inspect(state, restarts=0):
    return [{"State": state, "RestartCount": restarts}]


def test_inspect_argv():
    assert probe.inspect_argv("jellyfin") == ["docker", "inspect", "jellyfin"]


def test_health_running_and_healthy_exits_zero():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Log": [{"Output": "ok\n"}],
            },
        }
    )
    text, code = probe.format_health(data, "jellyfin")
    assert code == 0
    assert "healthy" in text and "running" in text


def test_health_unhealthy_exits_one_and_shows_streak_and_last_log():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 3,
                "Log": [{"Output": "connection refused\n"}],
            },
        }
    )
    text, code = probe.format_health(data, "qbittorrent")
    assert code == 1
    assert "unhealthy" in text and "3" in text and "connection refused" in text


def test_health_no_healthcheck_running_exits_zero():
    text, code = probe.format_health(_inspect({"Status": "running"}), "valheim")
    assert code == 0
    assert "no healthcheck" in text


def test_health_exited_exits_one():
    text, code = probe.format_health(_inspect({"Status": "exited"}), "valheim")
    assert code == 1
    assert "exited" in text


def test_health_not_found_exits_one():
    text, code = probe.format_health([], "nope")
    assert code == 1
    assert "not found" in text


#
# `health` ran `docker inspect` unconditionally until 2026-08-16 and had been dead on both
# cluster nodes since the 2026-08-14 Docker retirement — neither has the binary, so it raised
# FileNotFoundError. Every case below is a way the k8s replacement could report healthy when it
# is not, which is the only direction that matters for a post-deploy gate.

_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def _deploy(generation=1, observed=1, replicas=1, updated=1, ready=1, available=1):
    return {
        "metadata": {"generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
        },
    }


def _pods(*containers):
    """containers: (name, restart_count, finished_at_or_None)."""
    return {
        "items": [
            {
                "metadata": {"name": "svc-abc"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": name,
                            "restartCount": count,
                            "lastState": (
                                {"terminated": {"finishedAt": finished}}
                                if finished
                                else {}
                            ),
                        }
                        for name, count, finished in containers
                    ]
                },
            }
        ]
    }


def test_k8s_health_rolled_out_and_quiet_exits_zero():
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 0, None)), "freshrss", _NOW
    )
    assert code == 0
    assert "1/1 ready" in text


def test_k8s_health_missing_deployment_exits_one():
    text, code = probe.format_k8s_health(None, None, "nope", _NOW)
    assert code == 1
    assert "no Deployment" in text


def test_k8s_health_stale_generation_exits_one():
    """The controller has not observed the spec change yet, so the OLD pod is what is ready."""
    text, code = probe.format_k8s_health(
        _deploy(generation=5, observed=4), _pods(("app", 0, None)), "freshrss", _NOW
    )
    assert code == 1
    assert "not observed yet" in text


def test_k8s_health_incomplete_rollout_exits_one():
    text, code = probe.format_k8s_health(
        _deploy(replicas=2, updated=1, ready=1, available=1),
        _pods(("app", 0, None)),
        "freshrss",
        _NOW,
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_recent_restart_exits_one_despite_being_ready():
    """The kube-state-metrics failure of 2026-08-07: a bad liveness probe passes READINESS,
    flips the Deployment to Available, and only then starts getting killed. Every
    readiness-derived field reads healthy while the pod crashloops."""
    just_now = "2026-08-16T11:59:30Z"
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 3, just_now)), "kube-state-metrics", _NOW
    )
    assert code == 1
    assert "RECENT RESTART" in text and "30s ago" in text


def test_k8s_health_old_restart_does_not_fail():
    """A pod that restarted last week and has been up since is healthy — restartCount alone
    would fail it forever."""
    last_week = "2026-08-09T12:00:00Z"
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 3, last_week)), "freshrss", _NOW
    )
    assert code == 0
    assert "restarts=3" in text


def test_k8s_health_unparseable_restart_timestamp_does_not_fail_open():
    """An unreadable finishedAt must count as RECENT, not as 'long ago'.

    Treating unknown as old is the one direction a gate must never fail. Reachable whenever
    kubectl's timestamp format shifts — fractional seconds, for instance, parse as None.
    """
    assert probe._seconds_since("not-a-timestamp", _NOW) is None
    assert probe._seconds_since(None, _NOW) is None

    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 1, "2026-08-16T11:59:30.123456Z")), "freshrss", _NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_restart_with_no_laststate_fails_closed():
    """restartCount > 0 with no terminated state is still an unexplained restart."""
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 2, None)), "freshrss", _NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_checks_every_container_in_the_pod():
    """A sidecar crashlooping while the main container is fine still fails the gate."""
    just_now = "2026-08-16T11:59:00Z"
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 0, None), ("sidecar", 9, just_now)), "n8n", _NOW
    )
    assert code == 1
    assert "sidecar" in text


def _daemonset(generation=1, observed=1, desired=2, updated=2, ready=2, available=2):
    return {
        "kind": "DaemonSet",
        "metadata": {"generation": generation},
        "status": {
            "observedGeneration": observed,
            "desiredNumberScheduled": desired,
            "updatedNumberScheduled": updated,
            "numberReady": ready,
            "numberAvailable": available,
        },
    }


def test_k8s_health_reads_a_daemonset():
    """Six workloads here are DaemonSets — promtail, node-exporter, the crowdsec node agent.
    They carry the same four numbers under different status field names."""
    text, code = probe.format_k8s_health(
        _daemonset(), _pods(("app", 0, None)), "promtail", _NOW
    )
    assert code == 0
    assert "2/2 ready" in text


def test_k8s_health_daemonset_missing_a_node_exits_one():
    """Scheduled on 2 nodes, ready on 1 — a Deployment's readyReplicas would read 0 here, so
    the field mapping has to be per-kind rather than a shared default."""
    text, code = probe.format_k8s_health(
        _daemonset(ready=1, available=1), _pods(("app", 0, None)), "promtail", _NOW
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_argv_can_ask_for_a_daemonset():
    assert "daemonset" in probe.k8s_deploy_argv("promtail", "homelab", kind="daemonset")


def test_k8s_health_argv_targets_the_named_namespace():
    assert probe.k8s_deploy_argv("freshrss", "homelab")[:4] == [
        "k3s",
        "kubectl",
        "-n",
        "homelab",
    ]
    assert "app=freshrss" in probe.k8s_pods_argv("freshrss", "homelab")
