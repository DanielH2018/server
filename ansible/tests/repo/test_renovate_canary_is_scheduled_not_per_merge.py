"""The unpinned Renovate config canary runs on a schedule, and the PR-side job still exists.

The push-to-master run of `renovate config validator` moved to a daily workflow on 2026-09-10
(#1269): per-merge bought nothing over daily, and the unpinned fetch fails for minutes after
each of Renovate's ~8 daily npm publishes. Two things must both stay true afterwards, and each
is easy to lose alone:

- ci.yml's job must NOT run on push (or the flake is back), yet MUST still run on a PR, because
  `renovate config validator` is a required merge context. A job that skipped on PRs too would
  leave every PR waiting on a context that never reports.
- Something else must carry the canary. Dropping the push trigger without the scheduled
  workflow would leave a config Renovate stops accepting undetected until renovate.json is next
  touched.

Both workflows run one script, so the retry cannot drift between them.

Run: uv run pytest ansible/tests/repo/test_renovate_canary_is_scheduled_not_per_merge.py
"""

from pathlib import Path

from _helpers import REPO
from lib import yaml_fast

CI = REPO / ".github" / "workflows" / "ci.yml"
CANARY = REPO / ".github" / "workflows" / "renovate-config-canary.yml"
SCRIPT = "scripts/validate/renovate_config.sh"


def _job(workflow: Path, job_id: str) -> dict:
    jobs = yaml_fast.safe_load(workflow.read_text())["jobs"]
    assert job_id in jobs, f"{workflow.name} no longer declares job {job_id!r}"
    return jobs[job_id]


def _run_lines(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_the_pr_job_skips_push_but_not_pull_request():
    job = _job(CI, "renovate-config")
    condition = str(job.get("if", ""))
    assert "push" in condition, (
        "renovate-config runs on push again; the publish-window flake is back"
    )
    assert "pull_request" not in condition, (
        "renovate-config must still run on a PR: it is a required merge context"
    )


def test_the_canary_is_scheduled_and_dispatchable():
    triggers = yaml_fast.safe_load(CANARY.read_text())[
        True
    ]  # PyYAML parses the `on:` key as True
    assert "schedule" in triggers and triggers["schedule"], "the canary has no schedule"
    assert "workflow_dispatch" in triggers, (
        "the canary cannot be run by hand after a config edit"
    )
    assert "push" not in triggers and "pull_request" not in triggers


def test_both_workflows_run_the_one_retrying_script():
    assert (REPO / SCRIPT).exists()
    for workflow, job_id in ((CI, "renovate-config"), (CANARY, "validate")):
        runs = _run_lines(_job(workflow, job_id))
        assert any(SCRIPT in line for line in runs), (
            f"{workflow.name} does not run {SCRIPT}"
        )
        assert not any("npx" in line for line in runs), (
            f"{workflow.name} still fetches Renovate through npx, outside the retry"
        )
