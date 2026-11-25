"""The master commit a head's green `ci.yml` PR run tested it against.

`stale_base.master_paths_since` judges a green head stale by what master changed since this
commit. A pull_request run checks out `refs/pull/N/merge`, which GitHub builds when the PR's
branch is pushed. A PR pushed after master moved was therefore tested against the newer
master, and the merge base alone would send it through a second CI run it did not need.
"""

import re
import subprocess


from lib.gh import gh as run_gh, gh_json as run_gh_json
from lib.json_types import as_list, as_object

from lib import host_modules  # noqa: F401
from ci_verdict import WorkflowRun

# The line `actions/checkout` logs for `refs/pull/N/merge`; <base> is the master commit the run
# tested against.
_MERGE_REF_LINE = re.compile(r"\bMerge ([0-9a-f]{40}) into ([0-9a-f]{40})\b")
# A job that never checked out (skipped by a path filter) has no merge line; try a few.
_JOBS_TRIED = 3


def tested_base(head: str, gh_json=run_gh_json, gh=run_gh) -> str | None:
    """The master commit `head`'s green `ci.yml` PR run was tested against; None if unreadable.

    No API field of the run, its check suite or the PR names that merge commit's base: the
    run's `pull_requests[].base.sha` tracks the PR's base as it stands now, and the PR's `merge_commit_sha` is rewritten as master moves.
    The one record is the checkout step's log line `Merge <head> into <base>`, so this reads
    the newest successful run's log for that line. Any failure answers None and the caller
    falls back to the merge base, which only ever updates a branch it did not need to.
    """
    try:
        listing = as_object(
            gh_json(
                "api",
                f"repos/{{owner}}/{{repo}}/actions/workflows/ci.yml/runs"
                f"?head_sha={head}&event=pull_request",
            ),
            "gh api ci.yml runs",
        )
        runs = [
            WorkflowRun.parse(run)
            for run in as_list(listing.get("workflow_runs"), "ci.yml workflow_runs")
        ]
        run_ids = [
            run.id
            for run in runs
            if run.id is not None
            and run.status == "completed"
            and run.conclusion == "success"
        ]
        if not run_ids:
            return None
        job_ids = as_list(
            gh_json(
                "api",
                f"repos/{{owner}}/{{repo}}/actions/runs/{run_ids[0]}/jobs",
                "--jq",
                '[.jobs[] | select(.conclusion == "success") | .id]',
            ),
            "gh api run jobs",
        )
        for job_id in job_ids[:_JOBS_TRIED]:
            log = gh(
                "api",
                "--allow-escape-sequences",
                f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs",
                timeout=120.0,
            ).stdout
            for tested_head, base in _MERGE_REF_LINE.findall(log):
                if tested_head == head:
                    return base
    except subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError:
        return None
    return None
