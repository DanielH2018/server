"""`probe.py health` passes a CronJob that its manifest suspends on purpose, and fails one that is not.

The restore drill ships `suspend: true` until its unsuspend PR (H2.3e). A suspended CronJob fires
no Job, so the gate read it as "no evidence it has ever run". The intent is in the rendered
manifest, so the gate passes a live-suspended CronJob only when the role's own render also says
`suspend: true`. A CronJob suspended live whose manifest says it should run is drift and fails.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cronjob_suspended.py
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from diagnostics.probe_lib import health, health_alerts
from diagnostics.probe_lib.health_cronjob import declared_suspended, format_suspended
from diagnostics.probe_lib.health_render import rendered_docs
from lib.kubectl import Tools

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
NAME = "homelab/longhorn-restore-drill"
_QUIET = health_alerts.PromTools(query=lambda promql: [], sleep=lambda s: None)


def _cronjob(suspend):
    spec = {"schedule": "10 4 * * *"}
    if suspend is not None:
        spec["suspend"] = suspend
    return {"metadata": {"name": "longhorn-restore-drill"}, "spec": spec}


def _role_health(cronjob, declared):
    checked = [("homelab", "longhorn-restore-drill", cronjob, None, None, None)]
    return health.format_role_cronjob_health(
        "longhorn-restore-drill", checked, NOW, declared
    )


def test_a_cronjob_suspended_live_and_in_the_manifest_passes():
    text, code = _role_health(_cronjob(True), frozenset({NAME}))
    assert code == 0, text
    assert f"{NAME}: suspended; nothing to gate until it is unsuspended" in text


def test_a_cronjob_suspended_live_but_not_in_the_manifest_fails():
    text, code = _role_health(_cronjob(True), frozenset())
    assert code == 1, text
    assert "does not declare `suspend: true`" in text


def test_an_unsuspended_cronjob_with_no_job_still_fails_the_gate():
    for suspend in (False, None):
        text, code = _role_health(_cronjob(suspend), frozenset({NAME}))
        assert code == 1, text
        assert "no Job found" in text


def test_a_missing_cronjob_is_not_a_suspended_one():
    assert format_suspended(NAME, None, frozenset({NAME})) is None
    text, code = _role_health(None, frozenset({NAME}))
    assert code == 1 and "no CronJob" in text


def test_the_restore_drills_render_declares_suspend_and_a_running_role_does_not():
    """Non-vacuity on the real tree: the named member is the drill's own CronJob."""
    drill = declared_suspended(rendered_docs("longhorn-restore-drill"), "homelab")
    assert drill == frozenset({NAME})
    reconcile = declared_suspended(
        rendered_docs("longhorn-reconcile"), "longhorn-system"
    )
    assert reconcile == frozenset()


def _tools(cronjob):
    """A kubectl boundary: prod identity, one CronJob, no Jobs and no Kustomization."""

    def run(argv, timeout):
        if "nodes" in argv:
            doc = {"items": [{"metadata": {"name": "daniel-box"}}]}
        elif "cronjob" in argv:
            doc = cronjob
        else:
            doc = {"items": []}
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), "")

    return Tools(
        run=run,
        find_tool=lambda name: "/usr/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def _run(cronjob):
    return health.run_health(
        "longhorn-restore-drill",
        cluster="prod",
        served="prod",
        tools=_tools(cronjob),
        prom=_QUIET,
    )


def test_run_health_passes_the_suspended_drill(capsys):
    assert _run(_cronjob(True)) == 0
    assert "suspended; nothing to gate" in capsys.readouterr().out


def test_run_health_fails_the_drill_when_it_is_not_suspended_and_has_never_run(capsys):
    assert _run(_cronjob(False)) == 1
    assert "FAILED the gate" in capsys.readouterr().out
