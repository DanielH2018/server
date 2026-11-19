"""The case jobs' homelab-tools cache, restored through the hand-off action, as census rows.

`test_census_rows_workflows.py` runs `HANDOFF_ROWS`. Each row's red half is the real file with
one property broken.
"""

import re

from _ci_workflow_rows import (
    BUILD_JOB,
    CASE_JOBS,
    CASES_ENTRY,
    CASES_STEP,
    HANDOFF,
    HANDOFF_USES,
    LEGS_JOB,
    ci_job,
    ci_row,
    drop_the_hand_off,
    edit_steps,
    named_steps,
)
from _row_table import Census, Subject, tracked
from _workflow_rows import CI, Workflow, judge, mutants, runs, steps, uses

BUILD_TOOLS_KEY = f"${{{{ needs.{BUILD_JOB}.outputs.tools-key }}}}"
SEED = 'seed=(--tools-cache "$RUNNER_TEMP/tools-cache")'

TAR_COMMAND = re.compile(r'host-packages-build\.tar" -C "\$RUNNER_TEMP" (.*)')


def _tools_cache_problems(wf: Workflow) -> list[str]:
    build = ci_job(wf, BUILD_JOB)
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
    for name in CASE_JOBS:
        found += _case_job_restore_problems(name, ci_job(wf, name))
    return found


def _case_job_restore_problems(name: str, job: dict) -> list[str]:
    calls = [s for s in steps(job) if uses(s, HANDOFF_USES)]
    if len(calls) != 1:
        return [f"{name} runs the hand-off action in {len(calls)} steps, not one"]
    found = []
    if calls[0].get("with") != {"tools-key": BUILD_TOOLS_KEY}:
        found.append(f"{name} passes the hand-off a key other than the build's own")
    cases = [s for s in steps(job) if CASES_ENTRY in runs(s)]
    if not cases:
        found.append(f"{name} runs no cases")
    hit = f"${{{{ steps.{calls[0].get('id')}.outputs.cache-hit }}}}"
    for step in cases:
        if (step.get("env") or {}).get("TOOLS_CACHE_HIT") != hit:
            found.append(f"{name}'s cases step does not read the hand-off's cache-hit")
        if SEED not in runs(step):
            found.append(f"{name}'s cases step does not seed from the restored cache")
    return found


def _handoff_restore_problems(action: Workflow) -> list[str]:
    restores = [
        s for s in steps(action.get("runs") or {}) if uses(s, "actions/cache/restore")
    ]
    if len(restores) != 1:
        return [f"the hand-off action restores in {len(restores)} steps, not one"]
    found = []
    if restores[0].get("with") != {
        "path": "${{ runner.temp }}/tools-cache",
        "key": "${{ inputs.tools-key }}",
    }:
        found.append(
            "the restore reads a path or key other than the caller's tools-key"
        )
    hit = ((action.get("outputs") or {}).get("cache-hit") or {}).get("value")
    if hit != f"${{{{ steps.{restores[0].get('id')}.outputs.cache-hit }}}}":
        found.append("the `cache-hit` output does not read the restore step")
    return found


def _tar_the_tools_cache(wf: Workflow) -> None:
    for step in steps(wf["jobs"][BUILD_JOB]):
        step["run"] = TAR_COMMAND.sub(lambda m: m.group(0) + " tools-cache", runs(step))


def _restore_another_key(wf: Workflow) -> None:
    for step in steps(wf["jobs"][LEGS_JOB]):
        if uses(step, HANDOFF_USES):
            step["with"]["tools-key"] = "tools-${{ runner.os }}"


def _restore_step(action: Workflow) -> dict:
    return next(s for s in steps(action["runs"]) if uses(s, "actions/cache/restore"))


HANDOFF_ROWS = (
    Census(
        **ci_row(
            name="ci-cases-legs-restore-the-cache-the-build-fetched",
            reason=(
                "A pin the build fetched is not requested again by a case job. Each case job "
                "restores it through `.github/actions/host-packages-handoff` under the "
                "build's own key, not from the hand-off tar."
            ),
            offence=judge(_tools_cache_problems),
            red=mutants(
                CI,
                a_build_that_fetches_outside_the_cache=edit_steps(
                    BUILD_JOB, '--cache "$RUNNER_TEMP/tools-cache"', "--no-cache"
                ),
                a_tar_that_carries_the_cache=_tar_the_tools_cache,
                a_build_that_stops_outputting_the_key=lambda wf: wf["jobs"][BUILD_JOB][
                    "outputs"
                ].pop("tools-key"),
                a_legs_restore_under_another_key=_restore_another_key,
                a_cases_step_that_ignores_the_cache_hit=lambda wf: [
                    s["env"].pop("TOOLS_CACHE_HIT")
                    for s in named_steps(wf["jobs"][LEGS_JOB], CASES_STEP)
                ],
                a_cases_step_that_does_not_seed=edit_steps(LEGS_JOB, SEED, "seed=()"),
                a_k3s_job_without_the_hand_off=drop_the_hand_off("k3s_download"),
            ),
        )
    ),
    Census(
        name="ci-hand-off-action-restores-under-the-callers-key",
        reason=(
            "The hand-off action restores the build's homelab-tools cache under the "
            "`tools-key` its caller passes, which is the key the build saved under, and its "
            "`cache-hit` output reports that restore."
        ),
        files=lambda: tracked(HANDOFF),
        must_find=frozenset({HANDOFF}),
        green=(Subject(HANDOFF),),
        offence=judge(_handoff_restore_problems),
        red=mutants(
            HANDOFF,
            a_restore_under_a_fixed_key=lambda action: _restore_step(action)[
                "with"
            ].update(key="tools-${{ runner.os }}"),
            a_cache_hit_from_another_step=lambda action: action["outputs"][
                "cache-hit"
            ].update(value="${{ steps.other.outputs.cache-hit }}"),
        ),
    ),
)
