"""`probe.py health <tag>` for a CronJob-only role: the Job-based verdict.

configarr and pi-peer-backup declare no Deployment/DaemonSet/StatefulSet, only a CronJob. Until
this path existed, `probe.py health` reported "declares no rollout-checkable workload" for
both, which deploy_detach_notify.py's NOT_APPLICABLE_MARKERS turns into a `skipped` verdict
rather than a checked one. Neither role's post-deploy state was ever actually read outside the
deploy-time k8s/cronjob-gate run.

The census that pins which roles are on this path is in `test_probe_health_resolver.py`, beside
the full-tree render it reads.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_cronjobs.py
"""

from _probe_health_fixtures import NOW, pods

from diagnostics.probe_lib import health, health_cronjob


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
    `kubectl create job --from=cronjob` always sets `controller: true` (verified live, see
    roles/k8s/cronjob-gate/CLAUDE.md)."""
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
    schedule -- the fallback this gate takes when the deploy-time k8s/cronjob-gate run hasn't
    landed yet, or hasn't been read yet."""
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
    from diagnostics.probe_lib import releases

    monkeypatch.setattr(releases, "RELEASE_DIR", tmp_path)
    (tmp_path / "configarr.json").write_text('{"applied_at": "2026-08-16T09:00:00Z"}')
    assert health._deploy_applied_at("configarr") == "2026-08-16T09:00:00Z"


def test_deploy_applied_at_is_none_when_unreadable(tmp_path, monkeypatch):
    from diagnostics.probe_lib import releases

    monkeypatch.setattr(releases, "RELEASE_DIR", tmp_path)
    assert health._deploy_applied_at("configarr") is None
