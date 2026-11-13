"""The bootstrap cases run in a CI job of their own, on every release the host_packages legs run (#7819).

Inside the legs they were the longest case group, 338-477 s against 214-271 s for the next, and
their offline builds shared the leg's CPUs with every other group. Running them in one release
only would drop coverage, which is the operator's call, so the job keeps the legs' matrix.
`test_every_ci_job_is_gated.py` holds the job inside the `prek` gate.

Run: uv run pytest tests/repo/test_ci_bootstrap_cases_own_job.py
"""

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CASES_ENTRY = "scripts/validate/host_package_cases.py"
BOOTSTRAP_FLAG = "--bootstrap"
LEGS_JOB = "host_packages"


def _jobs() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _bootstrap_jobs() -> list[str]:
    return [
        job_id
        for job_id, job in _jobs().items()
        for step in job.get("steps") or ()
        if CASES_ENTRY in str(step.get("run", ""))
        and BOOTSTRAP_FLAG in str(step.get("run", "")).split()
    ]


def _releases(job: dict) -> list[str]:
    return sorted({leg["release"] for leg in job["strategy"]["matrix"]["include"]})


def test_only_host_packages_bootstrap_runs_the_bootstrap_cases():
    assert _bootstrap_jobs() == ["host_packages_bootstrap"]


def test_the_bootstrap_job_covers_every_release_the_legs_run():
    jobs = _jobs()
    legs = _releases(jobs[LEGS_JOB])
    assert len(legs) >= 2, f"{LEGS_JOB} lost a matrix leg: {legs}"
    assert {job_id: _releases(jobs[job_id]) for job_id in _bootstrap_jobs()} == {
        "host_packages_bootstrap": legs
    }


def test_the_bootstrap_jobs_steps_run_on_the_legs_conditions():
    """`test_host_package_cases_run_on_their_inputs.py` simulates the legs' `if:`s alone."""
    jobs = _jobs()
    legs = {step.get("name"): step.get("if") for step in jobs[LEGS_JOB]["steps"]}
    legs_cases = next(
        step.get("if")
        for step in jobs[LEGS_JOB]["steps"]
        if CASES_ENTRY in str(step.get("run", ""))
    )
    drifted = {}
    for step in jobs["host_packages_bootstrap"]["steps"]:
        name = step.get("name")
        runs_cases = CASES_ENTRY in str(step.get("run", ""))
        expected = legs_cases if runs_cases else legs.get(name, step.get("if"))
        if step.get("if") != expected:
            drifted[name] = step.get("if")
    assert len(jobs["host_packages_bootstrap"]["steps"]) >= 6
    assert drifted == {}
    assert jobs["host_packages_bootstrap"]["if"] == jobs[LEGS_JOB]["if"]
