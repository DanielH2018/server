"""Startup-time derivation, and the two traps that silently corrupt it.

The first version of this measured from `creationTimestamp` and reported a 31-hour startup for
every pod that had restarted, which poisoned the fleet aggregates rather than looking wrong. The
restarted-pod case below is that bug, pinned.
"""

from __future__ import annotations

from startup_baseline import has_readiness_probe, startup_seconds


def _pod(
    *, created, started, ready_at, phase="Running", owner_kind="ReplicaSet", probe=True
):
    container = {"name": "app"}
    if probe:
        container["readinessProbe"] = {"httpGet": {"path": "/", "port": 80}}
    return {
        "metadata": {
            "name": "svc-abc123-xyz",
            "creationTimestamp": created,
            "ownerReferences": [{"kind": owner_kind, "name": "svc-abc123"}],
        },
        "spec": {"containers": [container]},
        "status": {
            "phase": phase,
            "containerStatuses": [{"state": {"running": {"startedAt": started}}}],
            "conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": ready_at}
            ],
        },
    }


def test_startup_is_measured_from_container_start():
    pod = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:00Z",
        ready_at="2026-08-16T10:00:12Z",
    )
    assert startup_seconds(pod) == 12.0


def test_restarted_pod_measures_the_restart_not_the_original_creation():
    """The bug this file exists for: creationTimestamp is days older than the running
    container after a restart, which reads as a 31-hour startup."""
    pod = _pod(
        created="2026-08-15T13:09:31Z",
        started="2026-08-16T07:38:48Z",
        ready_at="2026-08-16T07:38:55Z",
    )
    assert startup_seconds(pod) == 7.0


def test_pod_that_is_not_running_is_skipped():
    pod = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:00Z",
        ready_at="2026-08-16T10:00:12Z",
        phase="Pending",
    )
    assert startup_seconds(pod) is None


def test_job_owned_pod_is_skipped():
    """One-shot probes and reconcilers have no rollout gap worth measuring."""
    pod = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:00Z",
        ready_at="2026-08-16T10:00:12Z",
        owner_kind="Job",
    )
    assert startup_seconds(pod) is None


def test_ready_before_container_start_is_rejected_rather_than_reported_negative():
    """A stale Ready condition from a previous container must not produce a negative time."""
    pod = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:30Z",
        ready_at="2026-08-16T10:00:12Z",
    )
    assert startup_seconds(pod) is None


def test_multi_container_pod_uses_the_last_container_to_start():
    pod = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:00Z",
        ready_at="2026-08-16T10:00:20Z",
    )
    pod["status"]["containerStatuses"].append(
        {"state": {"running": {"startedAt": "2026-08-16T10:00:05Z"}}}
    )
    assert startup_seconds(pod) == 15.0


def test_probe_presence_is_reported():
    with_probe = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:00Z",
        ready_at="2026-08-16T10:00:01Z",
    )
    without = _pod(
        created="2026-08-16T10:00:00Z",
        started="2026-08-16T10:00:00Z",
        ready_at="2026-08-16T10:00:01Z",
        probe=False,
    )
    assert has_readiness_probe(with_probe) is True
    assert has_readiness_probe(without) is False
