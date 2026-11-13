"""A CI leg runs the bootstrap cases where a k3s release outage cannot fail the PR (#7754).

The gating `host_packages` legs seed the k3s binary, so they never exercise install.sh's own
download. The leg that does must sit outside the verdict: a step marked `continue-on-error`, a
job the `prek` gate does not need, or a workflow of its own. `hosts/lib/ci_verdict.py` reads the
check-runs of the gate's `needs`, so a job-level `continue-on-error` inside them still gates.

Run: uv run pytest tests/repo/test_ci_k3s_download_leg_does_not_gate.py
"""

from _helpers import REPO
from lib import yaml_fast

WORKFLOWS = REPO / ".github" / "workflows"
CASES_ENTRY = "scripts/validate/host_package_cases.py"
GATE_JOB = "prek"


def _case_invocations() -> list[tuple[str, str, str, bool]]:
    """Each step that runs the cases: (workflow, job id, step name, whether it gates)."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        jobs = yaml_fast.safe_load(path.read_text()).get("jobs") or {}
        gate_needs = set(jobs.get(GATE_JOB, {}).get("needs") or ())
        for job_id, job in jobs.items():
            for step in job.get("steps") or ():
                if CASES_ENTRY not in str(step.get("run", "")):
                    continue
                gates = (
                    path.name == "ci.yml"
                    and job_id in gate_needs
                    and step.get("continue-on-error") is not True
                )
                found.append((path.name, job_id, step.get("name", ""), gates))
    return found


def test_a_non_gating_ci_step_runs_the_bootstrap_cases():
    invocations = _case_invocations()
    assert ("ci.yml", "host_packages") in {(w, j) for w, j, _, g in invocations if g}, (
        f"no gating step runs {CASES_ENTRY}: {invocations}"
    )
    non_gating = [(w, j, s) for w, j, s, gates in invocations if not gates]
    assert non_gating, (
        f"every step that runs {CASES_ENTRY} gates the PR, so none can leave k3s unseeded"
        f" without a release outage failing the run: {invocations}"
    )
