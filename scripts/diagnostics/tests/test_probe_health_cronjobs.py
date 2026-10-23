"""`probe.py health <tag>` for a CronJob-only role: the Job-based verdict.

configarr and pi-peer-backup declare no Deployment/DaemonSet/StatefulSet, only a CronJob. Until
this path existed, `probe.py health` reported "declares no rollout-checkable workload" for
both, which deploy_lib/detach_notify.py's NOT_APPLICABLE_MARKERS turns into a `skipped` verdict
rather than a checked one. Neither role's post-deploy state was ever actually read outside the
deploy-time k8s/cronjob-gate run.

The census that pins which roles are on this path is in `test_probe_health_resolver.py`, beside
the full-tree render it reads.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cronjobs.py
"""

import json
import subprocess
from pathlib import Path

from _probe_health_fixtures import NOW, pods

from diagnostics.probe_lib import health, health_cronjob
from diagnostics.probe_lib.health_kubectl import (
    k8s_cronjob_args,
    k8s_job_pods_args,
    k8s_jobs_args,
)
from lib import kubectl


def _cronjob(schedule="30 4 * * *"):
    return {"spec": {"schedule": schedule}}


def _job_doc(name, created, succeeded=0, failed=0):
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "status": {"succeeded": succeeded, "failed": failed},
    }


def _jobs_doc(cronjob_name, *jobs):
    """jobs: (name, created, succeeded, failed) tuples, each owned by `cronjob_name`."""
    return {
        "items": [
            {
                "metadata": {
                    "name": name,
                    "creationTimestamp": created,
                    "ownerReferences": [
                        {"kind": "CronJob", "name": cronjob_name, "controller": True}
                    ],
                },
                "status": {"succeeded": succeeded, "failed": failed},
            }
            for name, created, succeeded, failed in jobs
        ]
    }


def test_latest_owned_job_picks_the_newest_by_creation_time():
    jobs = _jobs_doc(
        "configarr",
        ("configarr-29123450", "2026-08-16T04:30:00Z", 1, 0),
        ("configarr-deploy-gate", "2026-08-16T09:00:00Z", 1, 0),
    )
    latest = health_cronjob.latest_owned_job(jobs, "configarr")
    assert latest["metadata"]["name"] == "configarr-deploy-gate"


def test_latest_owned_job_ignores_a_different_cronjobs_job():
    jobs = _jobs_doc("other", ("other-deploy-gate", "2026-08-16T11:00:00Z", 1, 0))
    assert health_cronjob.latest_owned_job(jobs, "configarr") is None


def test_latest_owned_job_ignores_a_non_controller_owner_reference():
    """A Job merely referencing the CronJob without `controller: true` is not one it created --
    `kubectl create job --from=cronjob` always sets `controller: true` (verified live
    2026-08-21)."""
    jobs = {
        "items": [
            {
                "metadata": {
                    "name": "unrelated",
                    "creationTimestamp": "2026-08-16T11:00:00Z",
                    "ownerReferences": [
                        {"kind": "CronJob", "name": "configarr", "controller": False}
                    ],
                },
                "status": {},
            }
        ]
    }
    assert health_cronjob.latest_owned_job(jobs, "configarr") is None


def test_latest_owned_job_is_none_with_no_jobs():
    assert health_cronjob.latest_owned_job({"items": []}, "configarr") is None
    assert health_cronjob.latest_owned_job(None, "configarr") is None


def test_job_outcome_succeeded_from_status_count():
    assert health_cronjob._job_outcome({"status": {"succeeded": 1}}) == "succeeded"


def test_job_outcome_succeeded_from_condition():
    job = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    assert health_cronjob._job_outcome(job) == "succeeded"


def test_job_outcome_failed():
    assert health_cronjob._job_outcome({"status": {"failed": 1}}) == "failed"


def test_job_outcome_running_with_no_terminal_state():
    assert health_cronjob._job_outcome({"status": {}}) == "running"
    assert health_cronjob._job_outcome({}) == "running"


def test_schedule_interval_daily():
    assert health_cronjob._schedule_interval_seconds("30 4 * * *") == 86400


def test_schedule_interval_weekly():
    assert health_cronjob._schedule_interval_seconds("30 4 * * 0") == 7 * 86400


def test_schedule_interval_unrecognised_shape_fails_closed_to_none():
    """Deliberately not a cron parser -- any shape but plain daily/weekly returns None, and
    format_cronjob_health's caller fails closed on that rather than guessing an interval."""
    assert health_cronjob._schedule_interval_seconds("*/15 * * * *") is None
    assert health_cronjob._schedule_interval_seconds("0 0 1 * *") is None
    assert health_cronjob._schedule_interval_seconds(None) is None
    assert health_cronjob._schedule_interval_seconds("") is None


def test_cronjob_health_no_cronjob_fails():
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr", None, None, None, None, NOW
    )
    assert code == 1
    assert "no CronJob" in text


def test_cronjob_health_no_job_fails():
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr", _cronjob(), None, None, None, NOW
    )
    assert code == 1
    assert "no evidence it has ever run" in text


def test_cronjob_health_fresh_success_passes():
    job = _job_doc("configarr-deploy-gate", "2026-08-16T11:00:00Z", succeeded=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr",
        _cronjob(),
        job,
        pods(("configarr", 0, None)),
        "2026-08-16T10:00:00Z",
        NOW,
    )
    assert code == 0
    assert "succeeded" in text


def test_cronjob_health_fresh_failure_fails():
    job = _job_doc("configarr-deploy-gate", "2026-08-16T11:00:00Z", failed=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr", _cronjob(), job, None, "2026-08-16T10:00:00Z", NOW
    )
    assert code == 1
    assert "FAILED" in text


def test_cronjob_health_still_running_fails():
    job = _job_doc("configarr-deploy-gate", "2026-08-16T11:59:00Z")
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr", _cronjob(), job, None, "2026-08-16T10:00:00Z", NOW
    )
    assert code == 1
    assert "has not finished" in text


def test_cronjob_health_restart_in_the_jobs_pod_fails():
    job = _job_doc("configarr-deploy-gate", "2026-08-16T11:00:00Z", succeeded=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr",
        _cronjob(),
        job,
        pods(("configarr", 1, None)),
        "2026-08-16T10:00:00Z",
        NOW,
    )
    assert code == 1
    assert "restarted" in text


def test_cronjob_health_stale_but_within_schedule_passes():
    """No run since the deploy, but the previous run succeeded recently against a daily
    schedule -- the fallback this gate takes when no owned run has landed since the deploy,
    or none has been read yet."""
    job = _job_doc("configarr-29123450", "2026-08-16T04:30:00Z", succeeded=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr",
        _cronjob("30 4 * * *"),
        job,
        pods(("configarr", 0, None)),
        "2026-08-16T09:00:00Z",
        NOW,
    )
    assert code == 0
    assert "within its" in text


def test_cronjob_health_stale_and_overdue_fails():
    job = _job_doc("configarr-29000000", "2026-08-13T04:30:00Z", succeeded=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr",
        _cronjob("30 4 * * *"),
        job,
        pods(("configarr", 0, None)),
        "2026-08-16T09:00:00Z",
        NOW,
    )
    assert code == 1
    assert "overdue" in text


def test_cronjob_health_stale_with_unrecognised_schedule_fails_closed():
    job = _job_doc("configarr-29000000", "2026-08-16T04:30:00Z", succeeded=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr",
        _cronjob("*/15 * * * *"),
        job,
        pods(("configarr", 0, None)),
        "2026-08-16T09:00:00Z",
        NOW,
    )
    assert code == 1
    assert "failing closed" in text


def test_cronjob_health_unreadable_job_creation_time_fails_closed():
    job = {
        "metadata": {"name": "configarr-deploy-gate", "creationTimestamp": "garbage"},
        "status": {"succeeded": 1},
    }
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr", _cronjob(), job, None, None, NOW
    )
    assert code == 1
    assert "failing closed" in text


def test_cronjob_health_no_deploy_stamp_checks_the_latest_job_directly():
    """An unreadable release stamp means "nothing to compare against", not "assume stale" --
    the schedule fallback exists for a Job that provably predates a KNOWN deploy time, not for
    the absence of one."""
    job = _job_doc("configarr-deploy-gate", "2026-08-16T11:00:00Z", succeeded=1)
    text, code = health_cronjob.format_cronjob_health(
        "homelab/configarr", _cronjob(), job, pods(("configarr", 0, None)), None, NOW
    )
    assert code == 0
    assert "since the last deploy" in text


def test_deploy_applied_at_reads_the_release_stamp(tmp_path, monkeypatch):
    from diagnostics.probe_lib import releases, releases_retired

    monkeypatch.setattr(releases, "RELEASE_DIR", tmp_path)
    # The owner map is fixed here, so the test does not follow which roles the tree hands to Flux.
    monkeypatch.setattr(
        releases_retired, "tree_owners", lambda host_vars=None: {"flux-svc": "flux"}
    )
    stamp = '{"applied_at": "2026-08-16T09:00:00Z"}'
    (tmp_path / "ansible-svc.json").write_text(stamp)
    (tmp_path / "flux-svc.json").write_text(stamp)
    assert health._deploy_applied_at("ansible-svc") == "2026-08-16T09:00:00Z"
    # A flux-owned service reads no record, though one is on disk.
    assert health._deploy_applied_at("flux-svc") is None
    # No record on disk reads as None too.
    assert health._deploy_applied_at("never-deployed") is None


# A Flux-owned role's gate Job (#5953). The `<role>-verify` Kustomization applies it, so it
# carries no ownerReferences, only the CronJob's jobTemplate labels and Flux's owner label
# (scripts/renderer/renderer_lib/cronjob_gate.py:gate_job). longhorn-reconcile's role and
# CronJob share a name, so its `-verify` Kustomization is `longhorn-reconcile-verify`.
FLUX_OWNER_LABEL = "kustomize.toolkit.fluxcd.io/name"
GATE = "longhorn-reconcile-deploy-gate-fda190fc"


def _flux_gate_job(name, created, app, kustomization, succeeded=1):
    labels = {"app": app}
    if kustomization is not None:
        labels[FLUX_OWNER_LABEL] = kustomization
    return {
        "metadata": {
            "name": name,
            "namespace": "longhorn-system",
            "creationTimestamp": created,
            "labels": labels,
        },
        "status": {"succeeded": succeeded, "failed": 0},
    }


def _reconcile_gate(created="2026-08-16T11:31:00Z", succeeded=1):
    return _flux_gate_job(
        GATE,
        created,
        "longhorn-reconcile",
        "longhorn-reconcile-verify",
        succeeded=succeeded,
    )


def _scheduled_reconcile_job(name, created):
    return _jobs_doc("longhorn-reconcile", (name, created, 1, 0))["items"][0]


def test_latest_owned_job_finds_a_flux_gate_job_with_no_owner_reference():
    jobs = {"items": [_reconcile_gate()]}
    latest = health_cronjob.latest_owned_job(jobs, "longhorn-reconcile")
    assert latest is not None
    assert latest["metadata"]["name"] == GATE


def test_latest_owned_job_ignores_another_cronjobs_flux_gate_job():
    """The decoy's CronJob name starts with this one's, so a prefix match would take it."""
    other = _flux_gate_job(
        "longhorn-reconcile-extra-deploy-gate-0badc0de",
        "2026-08-16T11:50:00Z",
        "longhorn-reconcile-extra",
        "longhorn-reconcile-extra-verify",
    )
    jobs = {"items": [_reconcile_gate(), other]}
    latest = health_cronjob.latest_owned_job(jobs, "longhorn-reconcile")
    assert latest is not None
    assert latest["metadata"]["name"] == GATE


def test_latest_owned_job_ignores_an_ownerless_job_not_applied_by_the_verify_kustomization():
    """An `app=<cronjob>` label alone is not a gate Job: only the `-verify` Kustomization's."""
    by_hand = _flux_gate_job(
        "longhorn-reconcile-manual", "2026-08-16T11:50:00Z", "longhorn-reconcile", None
    )
    by_main = _flux_gate_job(
        "longhorn-reconcile-other",
        "2026-08-16T11:55:00Z",
        "longhorn-reconcile",
        "longhorn-reconcile",
    )
    jobs = {"items": [_reconcile_gate(), by_hand, by_main]}
    latest = health_cronjob.latest_owned_job(jobs, "longhorn-reconcile")
    assert latest is not None
    assert latest["metadata"]["name"] == GATE


def test_latest_owned_job_prefers_a_newer_flux_gate_job_over_an_older_scheduled_firing():
    jobs = {
        "items": [
            _scheduled_reconcile_job(
                "longhorn-reconcile-29123457", "2026-08-16T11:17:00Z"
            ),
            _reconcile_gate("2026-08-16T11:31:00Z"),
        ]
    }
    latest = health_cronjob.latest_owned_job(jobs, "longhorn-reconcile")
    assert latest["metadata"]["name"] == GATE


def _reconcile_tools(jobs):
    """A `lib.kubectl.Tools` serving prod's nodes, the hourly CronJob, `jobs` and gate pods."""
    nodes = {
        "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
    }
    cronjob = {
        "metadata": {"name": "longhorn-reconcile", "namespace": "longhorn-system"},
        "spec": {"schedule": "17 * * * *"},
    }
    served = {
        ("get", "nodes", "-o", "json"): nodes,
        tuple(k8s_cronjob_args("longhorn-reconcile", "longhorn-system")): cronjob,
        tuple(k8s_jobs_args("longhorn-system")): jobs,
    }
    for job in jobs["items"]:
        job_name = job["metadata"]["name"]
        served[tuple(k8s_job_pods_args(job_name, "longhorn-system"))] = pods(
            ("longhorn-reconcile", 0, None)
        )

    def run(argv, timeout):
        # kubectl_argv puts `<binary> --kubeconfig <path>` ahead of the arguments.
        doc = served.get(tuple(argv[3:]))
        if doc is None:
            return subprocess.CompletedProcess(
                argv, 1, "", 'Error from server (NotFound): not found "x"'
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), "")

    return kubectl.Tools(
        run=run,
        find_tool=lambda name: "/usr/local/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def _reconcile_health(jobs, deploy_applied_at):
    tools = _reconcile_tools(jobs)
    cronjob, latest_job, job_pods, _ = health._fetch_cronjob(
        "longhorn-reconcile", "longhorn-reconcile", "longhorn-system", "prod", tools
    )
    checked = [
        (
            "longhorn-system",
            "longhorn-reconcile",
            cronjob,
            latest_job,
            job_pods,
            deploy_applied_at,
        )
    ]
    return health.format_role_cronjob_health("longhorn-reconcile", checked, NOW)


def test_health_passes_right_after_a_deploy_before_the_next_hourly_firing():
    """The Verify-by: deployed 11:30, gate run 11:31, last scheduled firing 11:17, now 12:00."""
    jobs = {
        "items": [
            _scheduled_reconcile_job(
                "longhorn-reconcile-29123457", "2026-08-16T11:17:00Z"
            ),
            _reconcile_gate("2026-08-16T11:31:00Z"),
        ]
    }
    text, code = _reconcile_health(jobs, "2026-08-16T11:30:00Z")
    assert code == 0, text
    assert GATE in text


def test_health_passes_for_a_new_flux_cronjob_role_before_its_first_firing():
    jobs = {"items": [_reconcile_gate("2026-08-16T11:31:00Z")]}
    text, code = _reconcile_health(jobs, "2026-08-16T11:30:00Z")
    assert code == 0, text
    assert GATE in text


def test_health_still_fails_a_flux_gate_job_that_predates_the_deploy():
    """The freshness comparison stays against the deploy time: a gate run from the previous
    revision proves nothing about this one."""
    jobs = {
        "items": [
            _scheduled_reconcile_job(
                "longhorn-reconcile-29123450", "2026-08-16T07:17:00Z"
            ),
            _reconcile_gate("2026-08-16T08:00:00Z"),
        ]
    }
    text, code = _reconcile_health(jobs, "2026-08-16T09:00:00Z")
    assert code == 1, text
    assert GATE in text


def test_latest_owned_job_ignores_a_gate_named_job_no_verify_kustomization_applied():
    """The label check on its own: each decoy has the exact gate name and is newer than GATE."""
    unlabelled = _flux_gate_job(
        "longhorn-reconcile-deploy-gate-deadbeef",
        "2026-08-16T11:50:00Z",
        "longhorn-reconcile",
        None,
    )
    by_main = _flux_gate_job(
        "longhorn-reconcile-deploy-gate-0badc0de",
        "2026-08-16T11:55:00Z",
        "longhorn-reconcile",
        "longhorn-reconcile",
    )
    jobs = {"items": [_reconcile_gate(), unlabelled, by_main]}
    latest = health_cronjob.latest_owned_job(jobs, "longhorn-reconcile")
    assert latest["metadata"]["name"] == GATE
