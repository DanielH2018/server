"""The CI job that runs the k3s release download case off the legs' critical path, as rows.

`test_census_rows_workflows.py` runs `K3S_DOWNLOAD_ROWS`. Each row's red half is the real file
with one property broken.
"""

from collections.abc import Callable

from _actions_expressions import condition, interpolate
from _ci_workflow_rows import (
    BUILD_JOB,
    CASES_ENTRY,
    GATE_JOB,
    LEGS_JOB,
    ci_job,
    ci_row,
)
from _row_table import Census
from _workflow_rows import CI, Workflow, jobs, judge, mutants, runs, steps

DOWNLOAD_FLAG = "--k3s-download"
DOWNLOAD_JOB = "k3s_download"
IMAGE_SCRIPT = "hosts/lib/tests/container/build-image.sh"

# (event, build result, render changed, cases changed, whether the job takes a runner).
RUNNER_CASES = (
    ("pull_request", "success", "false", "false", False),
    ("push", "success", "false", "false", False),
    ("pull_request", "success", "true", "false", True),
    ("pull_request", "success", "false", "true", True),
    ("workflow_dispatch", "success", "false", "false", True),
    ("pull_request", "failure", "true", "true", False),
)


def _runs_download_case(step: dict) -> bool:
    return CASES_ENTRY in runs(step) and DOWNLOAD_FLAG in runs(step)


def _download_jobs(wf: Workflow) -> list[str]:
    return [
        job_id
        for job_id, job in jobs(wf).items()
        if any(_runs_download_case(step) for step in steps(job))
    ]


def _placement_problems(wf: Workflow) -> list[str]:
    found = []
    if not any(CASES_ENTRY in runs(s) for s in steps(ci_job(wf, LEGS_JOB))):
        found.append(f"{LEGS_JOB} no longer runs {CASES_ENTRY} at all")
    if _download_jobs(wf) != [DOWNLOAD_JOB]:
        found.append(
            f"the jobs running {CASES_ENTRY} {DOWNLOAD_FLAG} are {_download_jobs(wf)}, "
            f"not [{DOWNLOAD_JOB!r}]"
        )
    needs = ci_job(wf, DOWNLOAD_JOB).get("needs")
    if needs != ["prove", BUILD_JOB]:
        found.append(f"{DOWNLOAD_JOB} needs {needs!r}, not prove and the build")
    return found


def _runner_problems(wf: Workflow) -> list[str]:
    job = ci_job(wf, DOWNLOAD_JOB)
    found = []
    for event, build, changed, cases, takes_a_runner in RUNNER_CASES:
        github = {"event_name": event}
        render = {"render": {"outputs": {"changed": changed, "cases": cases}}}
        run_cases = interpolate(
            ci_job(wf, BUILD_JOB)["outputs"]["run-cases"],
            {"github": github, "steps": render},
        )
        context = {
            "github": github,
            "needs": {
                # `prove` runs on a push alone; an unproven push leaves `proven` false.
                "prove": {
                    "result": "success" if event == "push" else "skipped",
                    "outputs": {"proven": "false" if event == "push" else ""},
                },
                BUILD_JOB: {"result": build, "outputs": {"run-cases": run_cases}},
            },
        }
        if condition(job, context) is not takes_a_runner:
            found.append(
                f"{DOWNLOAD_JOB} {'skips' if takes_a_runner else 'takes a runner'} on a "
                f"{event} with build={build}, changed={changed}, cases={cases}"
            )
    return found


def _image_or_case_steps(job: dict) -> list[dict]:
    return [s for s in steps(job) if _runs_download_case(s) or IMAGE_SCRIPT in runs(s)]


def _non_gating_problems(wf: Workflow) -> list[str]:
    job = ci_job(wf, DOWNLOAD_JOB)
    found = []
    if job.get("continue-on-error") is not True:
        found.append(f"{DOWNLOAD_JOB} lacks a job-level `continue-on-error: true`")
    if DOWNLOAD_JOB in (ci_job(wf, GATE_JOB).get("needs") or []):
        found.append(f"{DOWNLOAD_JOB} is in {GATE_JOB}'s needs")
    image_or_case = _image_or_case_steps(job)
    if len(image_or_case) != 2:
        found.append(
            f"{DOWNLOAD_JOB} has {len(image_or_case)} image or case steps, not 2"
        )
    found += [
        f"{s.get('name')!r} turns the job's check-run red"
        for s in image_or_case
        if s.get("continue-on-error") is not True
    ]
    return found


def _image_problems(wf: Workflow) -> list[str]:
    job = ci_job(wf, DOWNLOAD_JOB)
    found = []
    if not any(IMAGE_SCRIPT in runs(s) for s in steps(job)):
        found.append(f"{DOWNLOAD_JOB} never runs {IMAGE_SCRIPT}")
    checks_digests = any(
        "RepoDigests" in runs(s) for s in steps(job) if _runs_download_case(s)
    )
    budget = job.get("timeout-minutes")
    legs_budget = ci_job(wf, LEGS_JOB).get("timeout-minutes") or 0
    if not checks_digests and (budget is None or int(budget) < int(legs_budget)):
        found.append(
            f"{DOWNLOAD_JOB} runs the case on a built image with a {budget}-minute budget, "
            f"under the legs' {legs_budget}"
        )
    return found


def _download_job_steps(edit: Callable[[list[dict]], list[dict]]):
    def mutate(wf: Workflow) -> None:
        job = wf["jobs"][DOWNLOAD_JOB]
        job["steps"] = edit(steps(job))

    return mutate


def _legs_run_the_download_case(wf: Workflow) -> None:
    for step in steps(wf["jobs"][LEGS_JOB]):
        if CASES_ENTRY in runs(step):
            step["run"] = runs(step).replace(
                CASES_ENTRY, f"{CASES_ENTRY} {DOWNLOAD_FLAG}"
            )


def _a_red_case_step(wf: Workflow) -> None:
    for step in _image_or_case_steps(wf["jobs"][DOWNLOAD_JOB]):
        if _runs_download_case(step):
            step.pop("continue-on-error")


K3S_DOWNLOAD_ROWS = (
    Census(
        **ci_row(
            name="ci-k3s-download-case-runs-in-its-own-job",
            reason=(
                "The non-gating k3s download case runs in a job of its own that needs only "
                "`prove` and the build, so it runs beside the `host_packages` legs. As a step "
                "of the 26.04 leg it ran after the gating cases and added 80-109 s to the "
                "longest leg of the PR."
            ),
            offence=judge(_placement_problems),
            red=mutants(
                CI,
                the_legs_run_the_download_case=_legs_run_the_download_case,
                the_job_waits_for_the_legs=lambda wf: wf["jobs"][DOWNLOAD_JOB].update(
                    {"needs": ["prove", BUILD_JOB, LEGS_JOB]}
                ),
                the_job_without_the_case=_download_job_steps(
                    lambda s: [x for x in s if not _runs_download_case(x)]
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-k3s-download-job-takes-a-runner-only-when-the-build-has-work",
            reason=(
                "A job that only skips its steps still holds one of the account's 20 runner "
                "slots (docs/ci-throughput.md). The job runs on a dispatch, or when the build "
                "succeeded and its `run-cases` output says a render or a case input changed."
            ),
            offence=judge(_runner_problems),
            red=mutants(
                CI,
                a_job_that_ignores_run_cases=lambda wf: wf["jobs"][DOWNLOAD_JOB].update(
                    {"if": f"${{{{ needs.{BUILD_JOB}.result == 'success' }}}}"}
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-k3s-download-job-cannot-fail-the-pr",
            reason=(
                "A GitHub release outage must not fail a PR, so the job sits outside the "
                "`prek` gate's needs, which `hosts/lib/ci_verdict.py` reads, with a job-level "
                "`continue-on-error`. Its image and case steps carry their own "
                "`continue-on-error`, because a job-level one still leaves a failed job's "
                "check-run red in the PR's rollup."
            ),
            offence=judge(_non_gating_problems),
            red=mutants(
                CI,
                a_job_without_continue_on_error=lambda wf: wf["jobs"][DOWNLOAD_JOB].pop(
                    "continue-on-error"
                ),
                a_job_inside_the_gate=lambda wf: wf["jobs"][GATE_JOB]["needs"].append(
                    DOWNLOAD_JOB
                ),
                a_case_step_that_turns_red=_a_red_case_step,
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-k3s-download-job-boots-the-pinned-image-within-its-budget",
            reason=(
                "The job builds or pulls the pinned test image as the legs do. A Dockerfile "
                "PR builds the image, so the job either skips the case on an image without "
                "RepoDigests or carries a budget at least the legs'."
            ),
            offence=judge(_image_problems),
            red=mutants(
                CI,
                a_job_without_the_image_step=_download_job_steps(
                    lambda s: [x for x in s if IMAGE_SCRIPT not in runs(x)]
                ),
                a_budget_under_the_legs=lambda wf: wf["jobs"][DOWNLOAD_JOB].update(
                    {"timeout-minutes": 10}
                ),
            ),
        )
    ),
)
