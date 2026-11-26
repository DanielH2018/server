"""The CI workflow's host-package case jobs and the hand-off action they call, as census rows.

`test_census_rows_workflows.py` runs `CI_ROWS`. Each row's red half is the real file with one
property broken.
"""

import re
from collections.abc import Callable

from _row_table import Census, Subject, tracked
from _workflow_rows import (
    runs,
    CI,
    Workflow,
    jobs,
    judge,
    load,
    mutants,
    steps,
    uses,
)

CASES_ENTRY = "scripts/validate/host_package_cases.py"
BOOTSTRAP_FLAG = "--bootstrap"
LEGS_JOB = "host_packages"
BOOTSTRAP_JOB = "host_packages_bootstrap"
BUILD_JOB = "host_packages_build"
GATE_JOB = "prek"
CASE_JOBS = (LEGS_JOB, BOOTSTRAP_JOB, "k3s_download")
HANDOFF = ".github/actions/host-packages-handoff/action.yml"
HANDOFF_USES = "$/.github/actions/host-packages-handoff"
CASES_STEP = "Run the updater cases in systemd containers"
BUILD_STEP = "Build both renders with the pinned nfpm"
IMAGE_STEP = "Build the test image for this release"
CASES_READ = "steps.render.outputs.cases == 'true'"
RUN_CASES_GATE = "needs.host_packages_build.outputs.run-cases == 'true'"
CASES_VERDICT = re.compile(
    r'cases=\$\(uv run python scripts/validate/host_packages\.py cases-changed "\$base" \\\n'
    r'\s+"\$RUNNER_TEMP/base-tree" --inputs "\$CASE_INPUTS"\)\n'
    r'echo "cases=\$cases" >> "\$GITHUB_OUTPUT"'
)


def ci_job(wf: Workflow, name: str) -> dict:
    return jobs(wf).get(name, {})


def named_steps(job: dict, name: str) -> list[dict]:
    return [s for s in steps(job) if s.get("name") == name]


def ci_row(**row) -> dict:
    return {
        "files": lambda: tracked(CI),
        "must_find": frozenset({CI}),
        "green": (Subject(CI),),
        **row,
    }


def edit_steps(job: str, find: str, replace: str) -> Callable[[Workflow], None]:
    """A mutation that rewrites `find` in the `run` and `if` of every step of `job`."""

    def mutate(wf: Workflow) -> None:
        for step in steps(wf["jobs"][job]):
            for key in ("run", "if"):
                if find in str(step.get(key, "")):
                    step[key] = step[key].replace(find, replace)

    return mutate


def _bootstrap_job_ids(wf: Workflow) -> list[str]:
    return [
        job_id
        for job_id, job in jobs(wf).items()
        for step in steps(job)
        if CASES_ENTRY in runs(step) and BOOTSTRAP_FLAG in runs(step).split()
    ]


def _releases(job: dict) -> list[str]:
    include = ((job.get("strategy") or {}).get("matrix") or {}).get("include") or []
    return sorted({leg["release"] for leg in include})


def _only_one_job_bootstraps(wf: Workflow) -> list[str]:
    found = _bootstrap_job_ids(wf)
    if found == [BOOTSTRAP_JOB]:
        return []
    return [
        f"the jobs that run the bootstrap cases are {found}, not [{BOOTSTRAP_JOB!r}]"
    ]


def _bootstrap_release_problems(wf: Workflow) -> list[str]:
    legs = _releases(ci_job(wf, LEGS_JOB))
    found = []
    if not legs:
        found.append(f"{LEGS_JOB} runs no release")
    covered = {
        job_id: _releases(ci_job(wf, job_id)) for job_id in _bootstrap_job_ids(wf)
    }
    if covered != {BOOTSTRAP_JOB: legs}:
        found.append(f"the bootstrap jobs cover {covered}, the legs run {legs}")
    return found


def _bootstrap_condition_problems(wf: Workflow) -> list[str]:
    legs, boot = ci_job(wf, LEGS_JOB), ci_job(wf, BOOTSTRAP_JOB)
    if not legs or not boot:
        return [f"{LEGS_JOB} or {BOOTSTRAP_JOB} is missing"]
    by_name = {s.get("name"): s.get("if") for s in steps(legs)}
    cases_if = [s.get("if") for s in steps(legs) if CASES_ENTRY in runs(s)]
    if len(cases_if) != 1:
        return [f"{LEGS_JOB} runs the cases in {len(cases_if)} steps, not one"]
    found = []
    for step in steps(boot):
        name = step.get("name")
        expected = cases_if[0] if CASES_ENTRY in runs(step) else by_name.get(name)
        if CASES_ENTRY not in runs(step) and name not in by_name:
            expected = step.get("if")
        if step.get("if") != expected:
            found.append(f"{name!r} runs on {step.get('if')!r}, not {expected!r}")
    if len(steps(boot)) < 5:
        found.append(f"{BOOTSTRAP_JOB} has {len(steps(boot))} steps, fewer than five")
    if boot.get("if") != legs.get("if"):
        found.append(f"{BOOTSTRAP_JOB} has a job `if` unlike the legs'")
    return found


def _cases_gate_problems(wf: Workflow) -> list[str]:
    build, legs = ci_job(wf, BUILD_JOB), ci_job(wf, LEGS_JOB)
    found = []
    renders = [s for s in steps(build) if s.get("id") == "render"]
    if len(renders) != 1 or not CASES_VERDICT.search(runs(renders[0])):
        found.append(
            "the render step does not write cases-changed's verdict to `cases`"
        )
    if CASES_READ not in str((build.get("outputs") or {}).get("run-cases")):
        found.append(f"the {BUILD_JOB} `run-cases` output does not read `cases`")
    for step in named_steps(build, BUILD_STEP):
        if CASES_READ not in str(step.get("if")):
            found.append(f"{BUILD_STEP!r} does not read `cases`")
    if RUN_CASES_GATE not in str(legs.get("if")):
        found.append(f"the {LEGS_JOB} job `if` does not read `run-cases`")
    for name in (IMAGE_STEP, CASES_STEP):
        found += [
            f"{name!r} has its own `if:`, so the job's `run-cases` gate no longer decides it"
            for step in named_steps(legs, name)
            if "if" in step
        ]
    return found


def _artifacts(job: dict, action: str) -> set[str]:
    """The artifact names a job's `actions/<action>-artifact` steps name."""
    return {
        str((step.get("with") or {}).get("name"))
        for step in steps(job)
        if uses(step, f"actions/{action}-artifact")
    }


def _downloads(job: dict) -> set[str]:
    """The artifact names a job downloads, itself or through the hand-off action."""
    found = _artifacts(job, "download")
    if any(uses(step, HANDOFF_USES) for step in steps(job)):
        found |= _artifacts(load(HANDOFF)["runs"], "download")
    return found


def _build_consumer_problems(wf: Workflow) -> list[str]:
    built = _artifacts(ci_job(wf, BUILD_JOB), "upload")
    found = []
    for consumer in (*CASE_JOBS, "host_tools_arm64"):
        job = ci_job(wf, consumer)
        if job.get("needs") != ["prove", BUILD_JOB]:
            found.append(
                f"{consumer} needs {job.get('needs')!r}, not prove and the build"
            )
        downloads = _downloads(job)
        if not downloads:
            found.append(f"{consumer} downloads nothing from {BUILD_JOB}")
        elif not downloads <= built:
            found.append(f"{consumer} downloads {downloads - built}, never uploaded")
    return found


def _no_release_at_all(wf: Workflow) -> None:
    for name in (LEGS_JOB, BOOTSTRAP_JOB):
        wf["jobs"][name]["strategy"]["matrix"]["include"] = []


def _cases_on_another_release(wf: Workflow) -> None:
    for leg in wf["jobs"][BOOTSTRAP_JOB]["strategy"]["matrix"]["include"]:
        leg["release"] = "28.04"


def _bootstrap_in_the_legs(wf: Workflow) -> None:
    for step in steps(wf["jobs"][LEGS_JOB]):
        if CASES_ENTRY in runs(step):
            step["run"] = runs(step).replace(CASES_ENTRY, f"{CASES_ENTRY} --bootstrap")


def _drop_bootstrap_cases(wf: Workflow) -> None:
    boot = wf["jobs"][BOOTSTRAP_JOB]
    boot["steps"] = [s for s in boot["steps"] if CASES_ENTRY not in runs(s)]


def _set_if(job: str, name: str, value: str) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        for step in named_steps(wf["jobs"][job], name):
            step["if"] = value

    return mutate


def _rename_an_upload(wf: Workflow) -> None:
    for step in steps(wf["jobs"][BUILD_JOB]):
        if uses(step, "actions/upload-artifact") and "hand-off" in step["name"]:
            step["with"]["name"] = "renamed-hand-off"


def drop_the_hand_off(job: str) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        wf["jobs"][job]["steps"] = [
            s for s in steps(wf["jobs"][job]) if not uses(s, HANDOFF_USES)
        ]

    return mutate


CI_ROWS = (
    Census(
        **ci_row(
            name="ci-bootstrap-cases-run-in-their-own-job",
            reason=(
                "The bootstrap cases run in the `host_packages_bootstrap` job and nowhere else. "
                "Inside the legs they were the longest case group, 338-477 s against 214-271 s "
                "for the next, and their offline builds shared the leg's CPUs with every other "
                "group. `tests/repo/ci_workflows/test_every_ci_job_is_gated.py` holds the job inside the "
                "`prek` gate."
            ),
            offence=judge(_only_one_job_bootstraps),
            red=mutants(
                CI,
                the_legs_run_the_bootstrap_cases=_bootstrap_in_the_legs,
                the_bootstrap_job_without_its_cases=_drop_bootstrap_cases,
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-bootstrap-job-covers-every-release-the-legs-run",
            reason=(
                "Running the bootstrap cases on fewer releases than the legs would drop "
                "coverage, which is the operator's call, so the job keeps the legs' matrix. "
                "The legs run at least one release, so the comparison cannot pass on two "
                "empty matrices."
            ),
            offence=judge(_bootstrap_release_problems),
            red=mutants(
                CI,
                the_legs_run_no_release=_no_release_at_all,
                a_bootstrap_job_on_another_release=_cases_on_another_release,
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-bootstrap-job-steps-run-on-the-legs-conditions",
            reason=(
                "Each bootstrap step runs on its same-named leg step's `if`, its cases step on "
                "the legs' cases step's, and the job on the legs' job `if`. The job keeps five "
                "steps or more. `ci-cases-gates-read-the-cases-changed-verdict` reads the legs' "
                "gates alone."
            ),
            offence=judge(_bootstrap_condition_problems),
            red=mutants(
                CI,
                a_step_on_its_own_condition=_set_if(
                    BOOTSTRAP_JOB, "Build the test image for this release", "false"
                ),
                a_job_condition_unlike_the_legs=lambda wf: wf["jobs"][
                    BOOTSTRAP_JOB
                ].update({"if": "${{ true }}"}),
                a_bootstrap_job_of_four_steps=lambda wf: wf["jobs"][
                    BOOTSTRAP_JOB
                ].update({"steps": wf["jobs"][BOOTSTRAP_JOB]["steps"][:4]}),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-cases-gates-read-the-cases-changed-verdict",
            reason=(
                "A PR that changes only the container cases' inputs runs those cases on both "
                "legs. `host_packages.py cases-changed` decides whether the PR changes an input "
                "of the cases, and `scripts/validate/tests/test_validate_host_packages.py` "
                "tests that decision. The render step writes the verdict to `cases`, and every "
                "gate between it and the cases reads that output. The image and cases steps "
                "carry no `if:` of their own, so the job's `run-cases` gate decides them."
            ),
            offence=judge(_cases_gate_problems),
            red=mutants(
                CI,
                a_render_step_that_drops_the_verdict=edit_steps(
                    BUILD_JOB, 'echo "cases=$cases"', 'echo "cases=false"'
                ),
                a_run_cases_output_that_ignores_cases=lambda wf: wf["jobs"][BUILD_JOB][
                    "outputs"
                ].update({"run-cases": "${{ steps.render.outputs.changed }}"}),
                a_build_step_that_ignores_cases=edit_steps(
                    BUILD_JOB, f" || {CASES_READ}", ""
                ),
                a_legs_job_that_ignores_run_cases=lambda wf: wf["jobs"][
                    LEGS_JOB
                ].update({"if": "${{ true }}"}),
                an_image_step_with_its_own_condition=_set_if(
                    LEGS_JOB, IMAGE_STEP, "always()"
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-arm64-job-and-cases-legs-wait-for-the-build-alone",
            reason=(
                "No consumer of the build waits for another, and each downloads only what "
                "the build uploads, itself or through `.github/actions/host-packages-handoff`."
            ),
            offence=judge(_build_consumer_problems),
            red=mutants(
                CI,
                a_legs_job_that_also_waits_for_the_arm64_job=lambda wf: wf["jobs"][
                    LEGS_JOB
                ].update({"needs": ["prove", BUILD_JOB, "host_tools_arm64"]}),
                an_arm64_job_without_the_build=lambda wf: wf["jobs"][
                    "host_tools_arm64"
                ].update({"needs": ["prove"]}),
                an_arm64_download_the_build_never_uploads=lambda wf: [
                    s["with"].update(name="never-uploaded")
                    for s in steps(wf["jobs"]["host_tools_arm64"])
                    if uses(s, "actions/download-artifact")
                ],
                an_upload_renamed_under_its_consumers=_rename_an_upload,
                a_legs_job_that_downloads_nothing=drop_the_hand_off(LEGS_JOB),
            ),
        )
    ),
)
