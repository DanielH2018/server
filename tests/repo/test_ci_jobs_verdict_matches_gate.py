"""The verdict readers' job list equals the CI gate's `needs`, and their rule equals its script.

WHY THIS IS A TEST. `ci_verdict.jobs_verdict` (hosts/lib) decides green or red from the check-runs of the jobs
the `prek` gate aggregates, so a landing does not wait on a runner for the gate job (#5507). It
holds a hand-written list of check-run names, and a drift in either direction is silent:

- A job added to the gate's `needs` and missing from the list is a job the readers never wait
  for, so a landing could deploy a SHA whose new job is red while the gate is still queued.
- A job renamed or removed in ci.yml and left in the list holds the verdict at `pending` for
  ever, so the readers fall back to waiting on the gate job, which is the lag this removes.

The list is checked against ci.yml, with each matrix leg expanded the way GitHub names its
check-run. The rule is checked by running the gate's own `run:` script against the same
combinations and requiring `jobs_verdict` to agree.

Run: uv run pytest tests/repo/test_ci_jobs_verdict_matches_gate.py
"""

import itertools
import re
import subprocess

import pytest
from _helpers import REPO
from ci_verdict import (
    CI_BUILD_JOB,
    CI_BUILD_READER_JOBS,
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    CI_SKIPPED_MATRIX_JOBS,
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
    """The cases `jobs_verdict` cannot tell from the gate, because a check-run does not carry a
    job output. The reader leans on the workflow for both.

    `prove` succeeded with `proven=false` and every dependent skipped: each dependent skips
    only on `proven == 'true'`. A job that reads the build's artifacts skipped beside a build
    that succeeded: it also skips when the build did not succeed, where the build's own
    check-run carries that verdict, and when the build's output gave it no work, and on nothing
    else. `test_ci_skip_proven_master.py` pins such a job's whole condition."""
    jobs = _jobs()
    (build_id,) = [i for i, j in jobs.items() if j.get("name") == CI_BUILD_JOB]
    for job_id in _gate_job()["needs"]:
        if job_id == "prove":
            continue
        job = jobs[job_id]
        needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
        assert needs[0] == "prove", job_id
        assert set(needs[1:]) <= set(_gate_job()["needs"]), job_id
        if build_id in needs:
            continue
        condition = str(job["if"]).strip().removeprefix("${{").removesuffix("}}")
        condition = condition.strip()
        # The draft arm adds no skip `jobs_verdict` can see: a draft PR's head is never a
        # master commit, and the gate skips with its dependents there (#5563).
        assert condition == (
            "(needs.prove.result == 'cancelled' || needs.prove.result == 'failure'"
            " || (!cancelled() && needs.prove.outputs.proven != 'true'))"
            " && github.event.pull_request.draft != true"
        ), job_id


def _all(result: str, **override: str) -> dict[str, str]:
    return {n: override.get(n, result) for n in sorted(CI_GATE_NEEDS_JOBS)}


def _gate_exit(
    prove: str,
    proven: str,
    results: dict[str, str],
    build_said: dict[str, str] | None = None,
) -> int:
    """Run the gate's own script. It reads one env var per `needs` job, not per matrix leg.

    `build_said` holds the build's outputs the script reads, by env name; an output left out is
    one the build never wrote.
    """
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
    env = {
        "PROVE": prove,
        "PROVEN": proven,
        "PATH": "/usr/bin:/bin",
        **per_job,
        **(build_said or {}),
    }
    return subprocess.run(["bash", "-c", check["run"]], env=env, timeout=30).returncode


def _runs(prove: str, results: dict[str, str]) -> list[dict]:
    """The check-runs as GitHub lists them.

    A matrix job whose legs all skipped is ONE run under the job's own name, which is what a
    job-level skip produces (run 43354559178). A table fed leg-named skips passed while the
    reader read every such run as `pending`.
    """

    def run(name, conclusion):
        return {"name": name, "status": "completed", "conclusion": conclusion}

    listed = dict(results)
    for name, legs in CI_SKIPPED_MATRIX_JOBS.items():
        if all(results[leg] == "skipped" for leg in legs):
            for leg in legs:
                del listed[leg]
            listed[name] = "skipped"
    return [run(CI_PROVE_JOB, prove)] + [run(n, c) for n, c in listed.items()]


@pytest.mark.parametrize(
    ("prove", "proven", "results"),
    [
        ("skipped", "", _all("success")),
        ("success", "false", _all("success")),
        ("success", "true", _all("skipped")),
        ("skipped", "", _all("skipped")),
        ("success", "", _all("success", **{"pytest (shard 3 of 6)": "skipped"})),
        ("success", "true", _all("skipped", host_tools_arm64="failure")),
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
    """`pass` exactly when the gate exits 0; a run the gate fails is never `pass`.

    The build's outputs are the ones a run with no work for its readers writes, because the
    reader cannot see them and leans on the workflow for that case.
    """
    gate_passes = _gate_exit(prove, proven, results, _NO_WORK) == 0
    verdict = jobs_verdict(_runs(prove, results))
    assert (verdict == "pass") is gate_passes, (prove, proven, verdict)
    if not gate_passes:
        assert verdict in ("fail", "pending")


# A run whose build had no work for its readers: each skipped at job level.
_IDLE = dict.fromkeys(CI_BUILD_READER_JOBS, "skipped")
_NO_WORK = {"RUN_CASES": "false", "ARM64_TOOLS": "false"}


def test_the_build_readers_are_the_gate_needs_that_wait_for_the_build():
    jobs = _jobs()
    (build_id,) = [i for i, j in jobs.items() if j.get("name") == CI_BUILD_JOB]
    readers: set[str] = set()
    for job_id in _gate_job()["needs"]:
        if build_id in jobs[job_id].get("needs", []):
            readers |= _check_run_names(job_id, jobs[job_id])
    assert readers == CI_BUILD_READER_JOBS


def test_the_skipped_matrix_names_are_the_gates_matrix_jobs():
    jobs = _jobs()
    assert {
        str(jobs[job_id].get("name", job_id)): _check_run_names(job_id, jobs[job_id])
        for job_id in _gate_job()["needs"]
        if (jobs[job_id].get("strategy") or {}).get("matrix")
    } == CI_SKIPPED_MATRIX_JOBS


def test_a_build_reader_with_no_work_passes_the_gate_and_the_reader():
    results = _all("success", **_IDLE)
    assert _gate_exit("skipped", "", results, _NO_WORK) == 0
    assert jobs_verdict(_runs("skipped", results)) == "pass"


@pytest.mark.parametrize(
    ("reader", "build_said"),
    [
        # The build had work for a reader, so a skipped one never ran what the change touched.
        ("skipped", {"RUN_CASES": "true", "ARM64_TOOLS": "false"}),
        ("skipped", {"RUN_CASES": "false", "ARM64_TOOLS": "true"}),
        # The build never wrote its outputs.
        ("skipped", {}),
        # No work excuses a skip, not a reader that started and did not finish.
        ("cancelled", _NO_WORK),
    ],
)
def test_the_gate_fails_a_build_reader_that_did_not_run_for_any_other_reason(
    reader, build_said
):
    results = _all("success", **dict.fromkeys(CI_BUILD_READER_JOBS, reader))
    assert _gate_exit("skipped", "", results, build_said) != 0
