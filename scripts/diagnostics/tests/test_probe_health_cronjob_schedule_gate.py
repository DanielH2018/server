"""`probe.py health` gates a Flux CronJob that no gate Job runs on its schedule instead (#6128).

The restore drill ships no `<cronjob>-deploy-gate-<hash>` Job, because a copy of it runs a real
restore. Its CronJob carries `homelab/gate: schedule`, and the gate judges it against the time
Flux first applied the revision: with no Job it passes for one schedule period and fails after,
and with a scheduled firing it reads that firing the way a release-record role's gate does.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cronjob_schedule_gate.py
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from diagnostics.probe_lib import health, health_alerts
from diagnostics.probe_lib.health_cronjob import (
    FluxRevision,
    declared_schedule_gated,
    flux_revision,
    revision_applied_at,
)
from diagnostics.probe_lib.health_render import rendered_docs
from lib.kubectl import Tools

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
NAME = "homelab/longhorn-restore-drill"
GATED = frozenset({NAME})
REVISION = "prod@sha256:94ea22bc"
_QUIET = health_alerts.PromTools(query=lambda promql: [], sleep=lambda s: None)
CRONJOB = {
    "metadata": {"name": "longhorn-restore-drill"},
    "spec": {"schedule": "10 4 * * *", "suspend": False},
}


def _iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _kustomization(applied):
    """Kustomization `longhorn-restore-drill` as kustomize-controller reports it live."""
    return {
        "metadata": {"annotations": {"homelab/content-digest": "52909571cef5"}},
        "status": {
            "lastAppliedRevision": REVISION,
            "history": [
                {"firstReconciled": _iso(applied), "metadata": {"revision": REVISION}},
                {
                    "firstReconciled": _iso(applied - timedelta(days=1)),
                    "metadata": {"revision": "prod@sha256:8f2f1584"},
                },
            ],
        },
    }


def _firing(created, succeeded=1):
    return {
        "metadata": {
            "name": "longhorn-restore-drill-29881690",
            "creationTimestamp": _iso(created),
            "ownerReferences": [
                {
                    "kind": "CronJob",
                    "name": "longhorn-restore-drill",
                    "controller": True,
                }
            ],
        },
        "status": {"succeeded": succeeded, "failed": 1 - succeeded},
    }


def _revision(applied):
    return FluxRevision("52909571", None, _iso(applied) if applied else None)


def _gate(latest_job, applied, gated=GATED):
    checked = [
        (
            "homelab",
            "longhorn-restore-drill",
            CRONJOB,
            latest_job,
            None,
            _revision(applied),
        )
    ]
    return health.format_role_cronjob_health(
        "longhorn-restore-drill", checked, NOW, frozenset(), gated
    )


def test_no_job_inside_one_schedule_period_of_the_apply_passes():
    text, code = _gate(None, NOW - timedelta(hours=23))
    assert code == 0, text
    assert "first scheduled run is still due" in text


def test_no_job_past_one_schedule_period_of_the_apply_fails():
    # fact: ansible/roles/k8s/longhorn-restore-drill/CLAUDE.md#What the drill is and what it writes (the autonomous-role contract)
    text, code = _gate(None, NOW - timedelta(hours=25))
    assert code == 1, text
    assert "its first scheduled run never came" in text


def test_a_scheduled_firing_with_no_gate_job_passes_under_the_marker():
    """The state measured live on 2026-10-25: the 04:10 firing, no gate Job."""
    text, code = _gate(_firing(NOW - timedelta(hours=8)), NOW - timedelta(hours=1))
    assert code == 0, text
    assert "within its '10 4 * * *' schedule" in text


def test_the_same_firing_without_the_marker_still_needs_a_gate_job():
    text, code = _gate(
        _firing(NOW - timedelta(hours=8)), NOW - timedelta(hours=1), frozenset()
    )
    assert code == 1, text
    assert "no gate Job carries the revision Flux applies" in text


def test_a_failed_firing_under_the_marker_fails():
    text, code = _gate(_firing(NOW - timedelta(hours=1), succeeded=0), NOW)
    assert code == 1, text
    assert "FAILED" in text


def test_no_recorded_apply_time_fails_closed():
    text, code = _gate(None, None)
    assert code == 1, text
    assert "records no apply time" in text


def test_the_apply_time_is_the_live_revisions_history_entry():
    applied = NOW - timedelta(hours=3)
    kustomization = _kustomization(applied)
    assert revision_applied_at(kustomization) == _iso(applied)
    assert flux_revision(kustomization, {"items": []}, "longhorn-restore-drill") == (
        FluxRevision("52909571", None, _iso(applied))
    )
    kustomization["status"]["lastAppliedRevision"] = "prod@sha256:unseen"
    assert revision_applied_at(kustomization) is None
    assert revision_applied_at(None) is None


def test_the_drills_render_carries_the_marker_and_configarrs_does_not():
    """Non-vacuity on the real tree: the named member is the drill's own CronJob."""
    assert (
        declared_schedule_gated(rendered_docs("longhorn-restore-drill"), "homelab")
        == GATED
    )
    assert declared_schedule_gated(rendered_docs("configarr"), "homelab") == frozenset()


def _tools(kustomization):
    """A kubectl boundary: prod identity, the drill's CronJob, no Jobs, `kustomization`."""

    def run(argv, timeout):
        if "nodes" in argv:
            doc = {"items": [{"metadata": {"name": "daniel-box"}}]}
        elif "cronjob" in argv:
            doc = CRONJOB
        elif any(a == "kustomizations.kustomize.toolkit.fluxcd.io" for a in argv):
            doc = kustomization
        else:
            doc = {"items": []}
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), "")

    return Tools(
        run=run,
        find_tool=lambda name: "/usr/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def test_run_health_routes_the_drill_through_the_schedule_gate(capsys):
    """The marker reaches the gate from the real render: the failure is the schedule gate's own.

    Without the routing the same cluster reads "no Job found", the message of #6128.
    """
    kustomization = _kustomization(NOW)
    del kustomization["status"]["history"]
    code = health.run_health(
        "longhorn-restore-drill",
        cluster="prod",
        served="prod",
        tools=_tools(kustomization),
        prom=_QUIET,
    )
    out = capsys.readouterr().out
    assert code == 1, out
    assert "records no apply time" in out
