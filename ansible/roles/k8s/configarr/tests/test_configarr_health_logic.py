import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "files"))
# configarr_status.py is deployed as a runtime sibling (tasks/main.yml copies it here from the
# Docker role), so the import resolves on the host. It does not in the repo, where the file still
# lives with its own tests — hence the second path insert.
sys.path.insert(
    0,
    os.path.join(_HERE, "..", "..", "..", "containers", "configarr", "files"),
)
from configarr_health_logic import (  # noqa: E402
    decide,
    finished_at,
    latest_finished,
    returncode,
)

HOUR = 3600.0
MAX_AGE = 26 * HOUR


def job(name, *, succeeded=1, completion=None, failed_at=None, label="configarr"):
    status = {}
    if succeeded:
        status["succeeded"] = succeeded
    if completion:
        status["completionTime"] = completion
    if failed_at:
        status["conditions"] = [
            {"type": "Failed", "status": "True", "lastTransitionTime": failed_at}
        ]
    meta = {"name": name}
    if label:
        meta["labels"] = {"app": label}
    return {"metadata": meta, "status": status}


def test_succeeded_job_with_clean_logs_is_up():
    ok, msg = decide(
        job("configarr-1", completion="2026-08-08T04:30:12Z"),
        "Sync started\n2 profiles updated\nDone",
        2 * HOUR,
        MAX_AGE,
    )
    assert ok
    assert "2.0h ago" in msg


def test_failed_job_is_down():
    ok, msg = decide(
        job("configarr-1", succeeded=0, failed_at="2026-08-08T04:30:12Z"),
        "Unable to reach http://radarr:7878",
        1 * HOUR,
        MAX_AGE,
    )
    assert not ok
    assert "exit 1" in msg


def test_error_line_on_a_succeeded_job_is_still_down():
    # The reason configarr_status exists: the 2026-06-10 recyclarr breakage exited 0 every night.
    ok, msg = decide(
        job("configarr-1", completion="2026-08-08T04:30:12Z"),
        "Loaded config\nERROR: invalid trash_id foo",
        1 * HOUR,
        MAX_AGE,
    )
    assert not ok
    assert "error" in msg.lower()


def test_no_job_at_all_is_down():
    ok, msg = decide(None, "", 0, MAX_AGE)
    assert not ok
    assert "no completed configarr Job" in msg


def test_stale_job_is_down_even_though_it_succeeded():
    # Retained history keeps a Succeeded Job readable forever; without the age gate a CronJob that
    # stopped firing weeks ago reports last month's success as current.
    ok, msg = decide(
        job("configarr-1", completion="2026-07-01T04:30:12Z"),
        "Done",
        40 * HOUR,
        MAX_AGE,
    )
    assert not ok
    assert "40.0h ago" in msg


def test_empty_logs_are_down_not_a_clean_sync():
    # evaluate(0, "") returns (True, 'configarr sync ok: (no output)') on its own — a verdict built
    # from nothing. Reachable once the pod behind a retained Job is garbage collected.
    ok, msg = decide(
        job("configarr-1", completion="2026-08-08T04:30:12Z"),
        "   \n",
        1 * HOUR,
        MAX_AGE,
    )
    assert not ok
    assert "no logs" in msg


def test_latest_finished_picks_by_finish_time_not_list_order():
    jobs = [
        job("configarr-new", completion="2026-08-08T04:30:12Z"),
        job("configarr-old", completion="2026-08-07T04:30:12Z"),
    ]
    assert latest_finished(jobs, "configarr")["metadata"]["name"] == "configarr-new"


def test_unfinished_and_foreign_jobs_are_ignored():
    jobs = [
        job("configarr-running", succeeded=0),
        job("build-n8n", completion="2026-08-08T05:00:00Z", label=None),
        job("other-cron-1", completion="2026-08-08T06:00:00Z", label="janitorr"),
        job("configarr-done", completion="2026-08-08T04:30:12Z"),
    ]
    assert latest_finished(jobs, "configarr")["metadata"]["name"] == "configarr-done"


def test_a_deploy_triggered_job_counts_even_though_the_cronjob_does_not_own_it():
    # `kubectl create job --from=cronjob/configarr` copies jobTemplate.metadata but sets no
    # ownerReference. Selecting on the owner would ignore every deploy-time reconcile.
    manual = job("configarr-deploy", completion="2026-08-08T09:00:00Z")
    assert (
        latest_finished([manual], "configarr")["metadata"]["name"] == "configarr-deploy"
    )


def test_latest_finished_is_none_when_only_a_run_is_in_flight():
    assert latest_finished([job("configarr-running", succeeded=0)], "configarr") is None


def test_failed_job_finish_time_comes_from_the_failed_condition():
    # completionTime is set only on success, so a failed Job would otherwise look ageless and the
    # freshness gate would never fire on the case that needs it most.
    failed = job("configarr-1", succeeded=0, failed_at="2026-08-08T04:31:00Z")
    assert finished_at(failed) == "2026-08-08T04:31:00Z"
    assert returncode(failed) == 1
