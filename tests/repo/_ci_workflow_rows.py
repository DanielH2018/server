"""The CI workflow's host-package case jobs, as census rows.

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

CASES_STEP = "Run the updater cases in systemd containers"
# The cases glob each side's updater .deb out of the build step's output and boot the image the
# image step tags, so a run that skips either fails the cases rather than running them.
BUILD_STEP = "Build both renders with the pinned nfpm"
IMAGE_STEP = "Build the test image for this release"
CASES_READ = "steps.render.outputs.cases == 'true'"
RUN_CASES_GATE = "needs.host_packages_build.outputs.run-cases == 'true'"
CASES_VERDICT = re.compile(
    r'cases=\$\(uv run python scripts/validate/host_packages\.py cases-changed "\$base" \\\n'
    r'\s+"\$RUNNER_TEMP/base-tree" --inputs "\$CASE_INPUTS"\)\n'
    r'echo "cases=\$cases" >> "\$GITHUB_OUTPUT"'
)
TAR_COMMAND = re.compile(r'host-packages-build\.tar" -C "\$RUNNER_TEMP" (.*)')


def ci_job(wf: Workflow, name: str) -> dict:
    return jobs(wf).get(name, {})


def _named(job: dict, name: str) -> list[dict]:
    return [s for s in steps(job) if s.get("name") == name]


def _row(**row) -> dict:
    return {
        "files": lambda: tracked(CI),
        "must_find": frozenset({CI}),
        "green": (Subject(CI),),
        **row,
    }


def _edit(job: str, find: str, replace: str) -> Callable[[Workflow], None]:
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
    if len(legs) < 2:
        found.append(f"{LEGS_JOB} lost a matrix leg: {legs}")
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
    if len(steps(boot)) < 6:
        found.append(f"{BOOTSTRAP_JOB} has {len(steps(boot))} steps, fewer than six")
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
    for step in _named(build, BUILD_STEP):
        if CASES_READ not in str(step.get("if")):
            found.append(f"{BUILD_STEP!r} does not read `cases`")
    if RUN_CASES_GATE not in str(legs.get("if")):
        found.append(f"the {LEGS_JOB} job `if` does not read `run-cases`")
    for name in (IMAGE_STEP, CASES_STEP):
        found += [
            f"{name!r} has its own `if:`, so the job's `run-cases` gate no longer decides it"
            for step in _named(legs, name)
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


def _build_consumer_problems(wf: Workflow) -> list[str]:
    built = _artifacts(ci_job(wf, BUILD_JOB), "upload")
    found = []
    for consumer in (LEGS_JOB, "host_tools_arm64"):
        job = ci_job(wf, consumer)
        if job.get("needs") != ["prove", BUILD_JOB]:
            found.append(
                f"{consumer} needs {job.get('needs')!r}, not prove and the build"
            )
        downloads = _artifacts(job, "download")
        if not downloads:
            found.append(f"{consumer} downloads nothing from {BUILD_JOB}")
        elif not downloads <= built:
            found.append(f"{consumer} downloads {downloads - built}, never uploaded")
    return found


def _tools_cache_problems(wf: Workflow) -> list[str]:
    build, legs = ci_job(wf, BUILD_JOB), ci_job(wf, LEGS_JOB)
    build_run = "\n".join(runs(s) for s in steps(build))
    found = []
    if '--cache "$RUNNER_TEMP/tools-cache"' not in build_run:
        found.append("the build no longer fetches into the tools-cache directory")
    tar = TAR_COMMAND.findall(build_run)
    if len(tar) != 1:
        found.append(f"the build has {len(tar)} hand-off tar commands, not one")
    elif "tools-cache" in tar[0]:
        found.append("the hand-off tar carries the tools cache")
    if (build.get("outputs") or {}).get("tools-key") != (
        "${{ steps.tools-key.outputs.key }}"
    ):
        found.append(f"{BUILD_JOB} no longer outputs `tools-key` from its key step")
    restores = [s for s in steps(legs) if s.get("id") == "tools-cache"]
    if len(restores) != 1:
        found.append(f"{LEGS_JOB} has {len(restores)} `tools-cache` steps, not one")
    else:
        if not uses(restores[0], "actions/cache/restore"):
            found.append("the legs' tools-cache step is not actions/cache/restore")
        if restores[0].get("with") != {
            "path": "${{ runner.temp }}/tools-cache",
            "key": f"${{{{ needs.{BUILD_JOB}.outputs.tools-key }}}}",
        }:
            found.append("the legs restore a path or key other than the build's own")
    cases = _named(legs, CASES_STEP)
    if len(cases) != 1:
        found.append(f"{LEGS_JOB} has {len(cases)} steps named {CASES_STEP!r}")
    else:
        hit = "${{ steps.tools-cache.outputs.cache-hit }}"
        if (cases[0].get("env") or {}).get("TOOLS_CACHE_HIT") != hit:
            found.append("the cases step does not read the restore's cache-hit")
        if 'seed=(--tools-cache "$RUNNER_TEMP/tools-cache")' not in runs(cases[0]):
            found.append("the cases step does not seed from the restored cache")
    return found


def _only_the_first_release(wf: Workflow) -> None:
    for name in (LEGS_JOB, BOOTSTRAP_JOB):
        matrix = wf["jobs"][name]["strategy"]["matrix"]
        matrix["include"] = [
            leg for leg in matrix["include"] if leg["release"] == "24.04"
        ]


def _cases_without_a_release(wf: Workflow) -> None:
    matrix = wf["jobs"][BOOTSTRAP_JOB]["strategy"]["matrix"]
    matrix["include"] = [leg for leg in matrix["include"] if leg["release"] == "24.04"]


def _bootstrap_in_the_legs(wf: Workflow) -> None:
    for step in steps(wf["jobs"][LEGS_JOB]):
        if CASES_ENTRY in runs(step):
            step["run"] = runs(step).replace(CASES_ENTRY, f"{CASES_ENTRY} --bootstrap")


def _drop_bootstrap_cases(wf: Workflow) -> None:
    boot = wf["jobs"][BOOTSTRAP_JOB]
    boot["steps"] = [s for s in boot["steps"] if CASES_ENTRY not in runs(s)]


def _set_if(job: str, name: str, value: str) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        for step in _named(wf["jobs"][job], name):
            step["if"] = value

    return mutate


def _rename_an_upload(wf: Workflow) -> None:
    for step in steps(wf["jobs"][BUILD_JOB]):
        if uses(step, "actions/upload-artifact") and "hand-off" in step["name"]:
            step["with"]["name"] = "renamed-hand-off"


def _tar_the_tools_cache(wf: Workflow) -> None:
    for step in steps(wf["jobs"][BUILD_JOB]):
        step["run"] = TAR_COMMAND.sub(lambda m: m.group(0) + " tools-cache", runs(step))


def _restore_another_key(wf: Workflow) -> None:
    for step in steps(wf["jobs"][LEGS_JOB]):
        if step.get("id") == "tools-cache":
            step["with"]["key"] = "tools-${{ runner.os }}"


CI_ROWS = (
    Census(
        **_row(
            name="ci-bootstrap-cases-run-in-their-own-job",
            reason=(
                "The bootstrap cases run in the `host_packages_bootstrap` job and nowhere else. "
                "Inside the legs they were the longest case group, 338-477 s against 214-271 s "
                "for the next, and their offline builds shared the leg's CPUs with every other "
                "group. `tests/repo/test_every_ci_job_is_gated.py` holds the job inside the "
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
        **_row(
            name="ci-bootstrap-job-covers-every-release-the-legs-run",
            reason=(
                "Running the bootstrap cases in one release only would drop coverage, which is "
                "the operator's call, so the job keeps the legs' matrix. The legs keep two "
                "releases or more, so the comparison cannot pass on a lone leg."
            ),
            offence=judge(_bootstrap_release_problems),
            red=mutants(
                CI,
                the_legs_lose_a_matrix_leg=_only_the_first_release,
                a_bootstrap_job_missing_a_release=_cases_without_a_release,
            ),
        )
    ),
    Census(
        **_row(
            name="ci-bootstrap-job-steps-run-on-the-legs-conditions",
            reason=(
                "Each bootstrap step runs on its same-named leg step's `if`, its cases step on "
                "the legs' cases step's, and the job on the legs' job `if`. The job keeps six "
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
                a_bootstrap_job_of_five_steps=lambda wf: wf["jobs"][
                    BOOTSTRAP_JOB
                ].update({"steps": wf["jobs"][BOOTSTRAP_JOB]["steps"][:5]}),
            ),
        )
    ),
    Census(
        **_row(
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
                a_render_step_that_drops_the_verdict=_edit(
                    BUILD_JOB, 'echo "cases=$cases"', 'echo "cases=false"'
                ),
                a_run_cases_output_that_ignores_cases=lambda wf: wf["jobs"][BUILD_JOB][
                    "outputs"
                ].update({"run-cases": "${{ steps.render.outputs.changed }}"}),
                a_build_step_that_ignores_cases=_edit(
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
        **_row(
            name="ci-arm64-job-and-cases-legs-wait-for-the-build-alone",
            reason=(
                "Neither consumer of the build waits for the other, and each downloads only "
                "what the build uploads."
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
                a_download_the_build_never_uploads=lambda wf: [
                    s["with"].update(name="never-uploaded")
                    for s in steps(wf["jobs"][LEGS_JOB])
                    if uses(s, "actions/download-artifact")
                ],
                an_upload_renamed_under_its_consumers=_rename_an_upload,
                a_legs_job_that_downloads_nothing=lambda wf: wf["jobs"][
                    LEGS_JOB
                ].update(
                    {
                        "steps": [
                            s
                            for s in wf["jobs"][LEGS_JOB]["steps"]
                            if not uses(s, "actions/download-artifact")
                        ]
                    }
                ),
            ),
        )
    ),
    Census(
        **_row(
            name="ci-cases-legs-restore-the-cache-the-build-fetched",
            reason=(
                "A pin the build fetched is not requested again by a cases leg. The legs "
                "restore it from actions/cache under the build's own key, not from the "
                "hand-off tar."
            ),
            offence=judge(_tools_cache_problems),
            red=mutants(
                CI,
                a_build_that_fetches_outside_the_cache=_edit(
                    BUILD_JOB, '--cache "$RUNNER_TEMP/tools-cache"', "--no-cache"
                ),
                a_tar_that_carries_the_cache=_tar_the_tools_cache,
                a_build_that_stops_outputting_the_key=lambda wf: wf["jobs"][BUILD_JOB][
                    "outputs"
                ].pop("tools-key"),
                a_legs_restore_under_another_key=_restore_another_key,
                a_cases_step_that_ignores_the_cache_hit=lambda wf: [
                    s["env"].pop("TOOLS_CACHE_HIT")
                    for s in _named(wf["jobs"][LEGS_JOB], CASES_STEP)
                ],
                a_cases_step_that_does_not_seed=_edit(
                    LEGS_JOB,
                    'seed=(--tools-cache "$RUNNER_TEMP/tools-cache")',
                    "seed=()",
                ),
            ),
        )
    ),
)
