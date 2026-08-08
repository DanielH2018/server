"""Pure decision core for the configarr health cron (configarr_health.py).

Split from the I/O shell so it stays stdlib-only, host-Python-floor clean (daniel-box runs
/usr/bin/python3 3.12 — see ansible/tests/test_host_scripts_py312.py) and unit-testable without a
cluster. The shell runs the kubectl reads and the Kuma push; everything that decides up-or-down
lives here.

Replaces the `ts`/`ok`/`msg` state file monitor-bridge used to read over a bind mount. That idiom
does not survive the port — the sync now runs on daniel-box and monitor-bridge runs on
daniel-server, which is exactly the coupling that left autofix-bridge's crons reaching into a host
their targets had left. The Job object IS the state file now, and it lives where the run does.

The verdict still goes through configarr_status.evaluate(), unchanged and with its own tests: the
exit code alone is not enough, which is the whole reason that module exists (recyclarr's
process-only healthcheck missed the 2026-06-10 v8 breakage because every failing sync exited 0).
"""

from __future__ import annotations

import configarr_status as cs

# A Job the CronJob controller created but which has neither succeeded nor failed yet. Excluded
# from the verdict rather than reported: a run in progress says nothing about whether the LAST one
# worked, and reporting it would blank the previous night's result for the length of a sync.
_UNFINISHED = object()


def finished_at(job) -> str:
    """Completion timestamp, or '' if the Job has not finished.

    `status.completionTime` is set only on success. A Job that failed has none, so fall back to
    the Failed condition's transition time — without it every failed sync would look ageless and
    the freshness gate would never fire on the one case it most needs to.
    """
    status = job.get("status") or {}
    if status.get("completionTime"):
        return status["completionTime"]
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return condition.get("lastTransitionTime") or ""
    return ""


def is_configarr_job(job, name: str) -> bool:
    """Selected by LABEL, not by ownerReference, and the difference is the deploy-time reconcile.

    A Job the CronJob controller creates is owned by it; one made with `kubectl create job
    --from=cronjob/configarr` is not, and that is how a config change applies at deploy instead of
    waiting for 04:30. An owner check would silently ignore those runs — so a deploy that fixed a
    broken sync would leave the monitor red until the next night. The jobTemplate labels both.
    """
    return (job.get("metadata", {}).get("labels") or {}).get("app") == name


def latest_finished(jobs, cronjob_name: str):
    """The most recently finished configarr Job, or None.

    Sorted by finish time rather than taking the last item: `kubectl get` returns creation order,
    and with concurrencyPolicy Forbid that is normally the same thing — but a manually triggered
    catch-up run makes them differ, and picking the wrong one reports a stale result as current.
    """
    candidates = [
        job for job in jobs if is_configarr_job(job, cronjob_name) and finished_at(job)
    ]
    if not candidates:
        return None
    return max(candidates, key=finished_at)


def returncode(job) -> int:
    """0 if the Job succeeded, 1 otherwise — the shape configarr_status.evaluate() expects."""
    return 0 if (job.get("status") or {}).get("succeeded") else 1


def decide(job, logs, age_s, max_age_s):
    """Whether configarr's last sync is healthy, as (ok, msg) for the Kuma push.

    Three gates, in order, and each covers a way the other two read green while the sync is dead:

    1. No finished Job at all. The CronJob can be suspended, unschedulable, or simply deleted, and
       none of that produces a failing run to notice — there is just nothing there.
    2. Too old. Retained history means the newest Job stays readable and keeps reporting its old
       success long after the schedule stopped firing. This is the gate monitor-bridge applied to
       the state file's `ts`, carried over.
    3. Empty logs. `evaluate(0, "")` returns (True, 'configarr sync ok: (no output)') — a clean
       verdict built from nothing. Logs go missing when the pod behind a retained Job is garbage
       collected, so this is reachable, and it must not read as success.
    """
    if job is None:
        return (
            False,
            "no completed configarr Job found (CronJob suspended or never ran?)",
        )
    if age_s > max_age_s:
        return False, "last configarr sync %.1fh ago (max %.1fh)" % (
            age_s / 3600.0,
            max_age_s / 3600.0,
        )
    if not (logs or "").strip():
        return False, "configarr Job %s has no logs (pod garbage collected?)" % job.get(
            "metadata", {}
        ).get("name", "?")

    ok, msg = cs.evaluate(returncode(job), logs)
    if ok:
        return True, "%s (%.1fh ago)" % (msg, age_s / 3600.0)
    return False, msg
