"""The verdict readers' job list equals the CI gate's `needs`, and their rule equals its script.

WHY THIS IS A TEST. `deploy_git.jobs_verdict` decides green or red from the check-runs of the jobs
the `prek` gate aggregates, so a landing does not wait on a runner for the gate job (#5507). It
holds a hand-written list of check-run names, and a drift in either direction is silent:

- A job added to the gate's `needs` and missing from the list is a job the readers never wait
  for, so a landing could deploy a SHA whose new job is red while the gate is still queued.
- A job renamed or removed in ci.yml and left in the list holds the verdict at `pending` for
  ever, so the readers fall back to waiting on the gate job, which is the lag this removes.

The list is checked against ci.yml, with each matrix leg expanded the way GitHub names its
check-run. The rule is checked by running the gate's own `run:` script against the same
combinations and requiring `jobs_verdict` to agree.

Run: uv run pytest ansible/tests/repo/test_ci_jobs_verdict_matches_gate.py
"""

import itertools
import re
import subprocess

import pytest
from _helpers import REPO
from deployer_lib.deploy_git import (
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    jobs_verdict,
)
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _check_run_names(job_id: str, job: dict) -> set[str]:
    """The check-run name(s) GitHub gives a job: its `name:` or id, once per matrix leg."""
    template = str(job.get("name", job_id))
    matrix = (job.get("strategy") or {}).get("matrix")
    if not matrix:
        return {template}
    include = matrix.get("include")
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    if include is not None and not axes:
        legs = [dict(entry) for entry in include]
    else:
        legs = [
            dict(zip(axes, combo, strict=True))
            for combo in itertools.product(*axes.values())
        ]
    names = set()
    for leg in legs:
        if "name" in job:
            name = re.sub(
                r"\$\{\{\s*matrix\.(\w+)\s*\}\}",
                lambda m, leg=leg: str(leg[m[1]]),
                template,
            )
            name = re.sub(r"\$\{\{\s*strategy\.job-total\s*\}\}", str(len(legs)), name)
        else:
            name = f"{template} ({', '.join(str(v) for v in leg.values())})"
        names.add(name)
    return names


def _gate_job() -> dict:
    return next(j for j in _jobs().values() if j.get("name") == CI_GATE_NAME)


def _gate_dependencies() -> set[str]:
    jobs = _jobs()
    names: set[str] = set()
    for job_id in _gate_job()["needs"]:
        names |= _check_run_names(job_id, jobs[job_id])
    return names


def test_the_readers_job_list_equals_the_gates_needs():
    assert _gate_dependencies() == CI_GATE_NEEDS_JOBS | {CI_PROVE_JOB}


def test_the_expansion_finds_the_matrix_legs_it_must():
    # The two matrix shapes ci.yml uses: a `name:` template over an axis, and an `include`
    # list on a job with no `name:`. A parse that dropped either would let the list shrink.
    assert "pytest (shard 6 of 6)" in _gate_dependencies()
    assert "host_packages (26.04, 3.14)" in _gate_dependencies()


def test_a_skipped_dependent_can_only_mean_the_tree_was_proven():
    """The one case `jobs_verdict` cannot tell from the gate: `prove` succeeded with
    `proven=false` and every dependent skipped. A check-run does not carry a job output, so the
    reader leans on the workflow: each dependent skips only on `proven == 'true'`."""
    jobs = _jobs()
    for job_id in _gate_job()["needs"]:
        if job_id == "prove":
            continue
        job = jobs[job_id]
        assert job["needs"] == "prove", job_id
        condition = (
            str(job["if"]).strip().removeprefix("${{").removesuffix("}}").strip()
        )
        # The draft arm adds no skip `jobs_verdict` can see: a draft PR's head is never a
        # master commit, and the gate skips with its dependents there (#5563).
        assert condition == (
            "(needs.prove.result == 'cancelled' || needs.prove.result == 'failure'"
            " || (!cancelled() && needs.prove.outputs.proven != 'true'))"
            " && github.event.pull_request.draft != true"
        ), job_id


def _all(result: str, **override: str) -> dict[str, str]:
    return {n: override.get(n, result) for n in sorted(CI_GATE_NEEDS_JOBS)}


def _gate_exit(prove: str, proven: str, results: dict[str, str]) -> int:
    """Run the gate's own script. It reads one env var per `needs` job, not per matrix leg."""
    jobs = _jobs()
    gate = _gate_job()
    check = next(s for s in gate["steps"] if "env" in s and "HOOKS" in s["env"])
    per_job = {}
    for var, expr in check["env"].items():
        m = re.search(r"needs\.(\w+)\.result", str(expr))
        if m and m[1] != "prove":
            legs = [results[n] for n in _check_run_names(m[1], jobs[m[1]])]
            # GitHub reports a matrix job as its worst leg.
            per_job[var] = next(
                (bad for bad in ("failure", "cancelled", "skipped") if bad in legs),
                "success",
            )
    env = {"PROVE": prove, "PROVEN": proven, "PATH": "/usr/bin:/bin", **per_job}
    return subprocess.run(["bash", "-c", check["run"]], env=env, timeout=30).returncode


def _runs(prove: str, results: dict[str, str]) -> list[dict]:
    def run(name, conclusion):
        return {"name": name, "status": "completed", "conclusion": conclusion}

    return [run(CI_PROVE_JOB, prove)] + [run(n, c) for n, c in results.items()]


@pytest.mark.parametrize(
    ("prove", "proven", "results"),
    [
        ("skipped", "", _all("success")),
        ("success", "false", _all("success")),
        ("success", "true", _all("skipped")),
        ("skipped", "", _all("skipped")),
        ("success", "", _all("success", **{"ansible-lint": "skipped"})),
        ("success", "true", _all("skipped", **{"ansible-lint": "failure"})),
        ("skipped", "", _all("success", **{"pytest (shard 3 of 6)": "failure"})),
        ("failure", "true", _all("skipped")),
        ("cancelled", "", _all("success")),
        ("cancelled", "true", _all("success")),
        ("cancelled", "true", _all("skipped")),
        ("failure", "", _all("success")),
        ("failure", "true", _all("success")),
    ],
)
def test_jobs_verdict_agrees_with_the_gates_own_script(prove, proven, results):
    """`pass` exactly when the gate exits 0; a run the gate fails is never `pass`."""
    gate_passes = _gate_exit(prove, proven, results) == 0
    verdict = jobs_verdict(_runs(prove, results))
    assert (verdict == "pass") is gate_passes, (prove, proven, verdict)
    if not gate_passes:
        assert verdict in ("fail", "pending")
