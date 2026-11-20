"""The CI workflow's dispatch sweep and its package-root scans, as census rows.

`test_census_rows_workflows.py` runs `SWEEP_ROWS`. Each row's red half is the real file with
one property broken.
"""

import re
from collections.abc import Callable

from _ci_workflow_rows import ci_row
from _row_table import Census
from _workflow_rows import CI, Workflow, jobs, judge, mutants, runs, steps, triggers

BUILD_JOB = "host_packages_build"
GATE = "steps.render.outputs.changed"
FIRST_SCAN = "Scan the named package roots for secrets"
SECOND_SCAN = "Check the named package roots' placeholders against their SOPS keys"
SCANS = {FIRST_SCAN: "scan-roots", SECOND_SCAN: "placeholders"}
FULL_SWEEP = "Run hooks (full sweep)"
REJECT = "Reject a PR whose merge changes no files"
NO_DISPATCH_ARM = (
    "${{ github.workflow }}-${{ github.event_name == 'pull_request' "
    "&& github.ref || github.sha }}"
)

# The census names its members. A step rename would otherwise drop the step out of the check
# without failing anything, and `all()` over an empty set passes.
SWEEP_STEPS = frozenset(
    {
        FULL_SWEEP,
        "Build both renders with the pinned nfpm",
        "Build the test image for this release",
        "Run the updater cases in systemd containers",
        "Green half — the all-digit values reach the cluster as their digits",
        "Red half — values encrypted from an int fail to apply or come back changed",
    }
)
SCOPED_STEPS = frozenset(
    {
        REJECT,
        "Notice: CLAUDE.md sections citing atoms this PR moved",
        "Notice step failed",
        "Upload notice failure",
        "Check whether this PR touches the type proof's inputs",
        "Render-diff gates",
        "Cluster pin checks",
    }
)
KNOWN_JOBS = frozenset(
    {
        "hooks",
        "pr_gates",
        "pytest",
        "sops_manifest_type_proof",
        "host_packages_build",
        "host_packages",
        "prek",
        "renovate-config",
        "prove",
    }
)
# `prove` proves a push's tree or a labelled PR's head, and its absence is what makes every
# other job run.
PUSH_ONLY_JOBS = frozenset({"prove"})

# The proof arm of a dependent job's condition. `prove` is skipped on a dispatch, so its output
# is empty and the comparison is true there; the evaluator reads the arm that way.
_PROOF_ARM = "needs.prove.outputs.proven != 'true'"
# A dispatch is not cancelled and `prove` is skipped, so the inner arm is true and so is the
# whole. It is replaced whole, because `truthy` splits on `||` and does not read parentheses.
_CANCEL_ARM = (
    "(needs.prove.result == 'cancelled' || needs.prove.result == 'failure'"
    " || (!cancelled() && needs.prove.outputs.proven != 'true'))"
)
# A dispatch carries no pull request, so the draft field is unset and the comparison is true.
_DRAFT_ARM = "github.event.pull_request.draft != true"
# The build runs on a dispatch, so this arm holds unless the build itself failed, and that
# failure already reds the sweep.
_BUILD_ARM = "needs.host_packages_build.result == 'success'"
# The build's `run-cases` output is true on a dispatch.
_RUN_CASES_ARM = "needs.host_packages_build.outputs.run-cases == 'true'"
_EVENT_TERM = re.compile(r"github\.event_name\s*(==|!=)\s*'([a-z_]+)'")


def truthy(condition: str, event: str) -> bool:
    """Evaluate the `github.event_name` subset of a GitHub expression for `event`.

    ci.yml's conditions are `github.event_name <op> '<literal>'` terms joined by `||` and
    `&&`, sometimes beside a step-output term. Any other term counts as false, which is what a
    step output is on a dispatch: the step that writes `steps.changed.outputs.mode` runs only
    on a pull request, so the output is unset and every comparison against it fails.
    """

    def term(text: str) -> bool:
        text = text.strip().removeprefix("${{").removesuffix("}}").strip()
        if text == "true":
            return True
        match = _EVENT_TERM.search(text)
        if not match:
            return False
        operator, literal = match.groups()
        return event == literal if operator == "==" else event != literal

    return any(
        all(term(part) for part in arm.split("&&")) for arm in condition.split("||")
    )


def _job_runs_on_dispatch(condition: str | None) -> bool:
    # `always()` is the `prek` gate: it runs whatever its dependencies did, by design.
    if condition is None or "always()" in condition:
        return True
    for arm in (_CANCEL_ARM, _PROOF_ARM, _DRAFT_ARM, _BUILD_ARM, _RUN_CASES_ARM):
        condition = condition.replace(arm, "true")
    return truthy(condition.replace("!cancelled()", "true"), "workflow_dispatch")


def _named_steps(wf: Workflow, name: str) -> list[dict]:
    return [s for job in jobs(wf).values() for s in steps(job) if s.get("name") == name]


def _step_conditions(wf: Workflow) -> dict[str, str]:
    """Every named step in ci.yml, keyed by step name. A step with no `if:` always runs."""
    return {
        step["name"]: str(step.get("if", "true"))
        for job in jobs(wf).values()
        for step in steps(job)
        if "name" in step
    }


def _dispatch_trigger_problems(wf: Workflow) -> list[str]:
    declared = triggers(wf)
    if not declared:
        return ["no triggers parsed out of ci.yml's `on:` mapping"]
    return [] if "workflow_dispatch" in declared else ["no workflow_dispatch trigger"]


def _dispatch_job_problems(wf: Workflow) -> list[str]:
    found = [f"job census lost {name}" for name in sorted(KNOWN_JOBS - jobs(wf).keys())]
    return found + [
        f"job {name} does not run on a workflow_dispatch"
        for name, job in jobs(wf).items()
        if name not in PUSH_ONLY_JOBS and not _job_runs_on_dispatch(job.get("if"))
    ]


def _dispatch_step_problems(wf: Workflow) -> list[str]:
    conditions = _step_conditions(wf)
    if lost := sorted((SWEEP_STEPS | SCOPED_STEPS) - conditions.keys()):
        return [f"step census lost {lost}; rename the entry with the step"]
    found = [
        f"step {name!r} does not fire on a workflow_dispatch"
        for name in sorted(SWEEP_STEPS)
        if not truthy(conditions[name], "workflow_dispatch")
    ]
    found += [
        f"step {name!r} fires on a workflow_dispatch"
        for name in sorted(SCOPED_STEPS)
        if truthy(conditions[name], "workflow_dispatch")
    ]
    # Non-vacuity: an evaluator that read everything as false would pass the scoped half.
    if not truthy(conditions[REJECT], "pull_request") or truthy(
        conditions[REJECT], "push"
    ):
        found.append(f"step {REJECT!r} does not fire on a pull request alone")
    return found


def _dispatch_group_problems(wf: Workflow) -> list[str]:
    group = str((wf.get("concurrency") or {}).get("group", ""))
    keyed = "workflow_dispatch" in group and "github.run_id" in group
    return [] if keyed else [f"concurrency group {group!r} has no dispatch arm"]


def _scan_problems(wf: Workflow) -> list[str]:
    job = steps(jobs(wf).get(BUILD_JOB, {}))
    names = [step.get("name") for step in job]
    ids = [step.get("id") for step in job]
    if "render" not in ids:
        return ["the job has no step with id `render`"]
    render = ids.index("render")
    gated = [i for i, step in enumerate(job) if GATE in str(step.get("if", ""))]
    found = []
    for name, command in SCANS.items():
        if name not in names:
            found.append(f"no step is named {name!r}")
            continue
        index = names.index(name)
        if index < render:
            found.append(f"{name!r} runs before the render")
        if "if" in job[index]:
            found.append(f"{name!r} has an `if:`, so it can skip")
        if gated and index > gated[0]:
            found.append(f"{name!r} comes after a step gated on {GATE}")
        if command not in runs(job[index]):
            found.append(f"{name!r} does not run `{command}`")
    return found


def _set_if(name: str, value: str | None) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        for step in _named_steps(wf, name):
            if value is None:
                step.pop("if", None)
            else:
                step["if"] = value

    return mutate


def _rename(old: str, new: str) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        for step in _named_steps(wf, old):
            step["name"] = new

    return mutate


def _edit_step(name: str, fields: dict) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        for step in _named_steps(wf, name):
            step.update(fields)

    return mutate


def _move_scan(name: str, to: int | None) -> Callable[[Workflow], None]:
    """Move the scan step to index `to`, or to the end of the job when `to` is None."""

    def mutate(wf: Workflow) -> None:
        job = jobs(wf)[BUILD_JOB]["steps"]
        step = job.pop(next(i for i, s in enumerate(job) if s.get("name") == name))
        job.insert(len(job) if to is None else to, step)

    return mutate


SWEEP_ROWS = (
    Census(
        **ci_row(
            name="ci-declares-the-dispatch-trigger",
            reason=(
                "`workflow_dispatch` is on ci.yml for one job: sweeping every hook against a "
                "branch whose own diff scopes them all out. A runner-image bump is the case. "
                "A workflow-only diff matches no hook's `files` regex, so the PR run is green "
                "without having run `validate-render-templates` or gitleaks on that image, and "
                "the only full sweep is the push run after the merge (#2366)."
            ),
            offence=judge(_dispatch_trigger_problems),
            red=mutants(
                CI,
                no_dispatch_trigger=lambda wf: triggers(wf).pop("workflow_dispatch"),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-dispatch-runs-every-job",
            reason=(
                "The dispatch trigger pays off only while every job reads a dispatch as a "
                "push. A dispatched sweep that skips a job reports green for the half it ran. "
                "`prove` is the one job meant to be skipped: it proves a push's tree or a "
                "labelled PR's head, and its absence is what makes every other job run "
                "(`test_ci_skip_proven_master.py` pins its condition)."
            ),
            offence=judge(_dispatch_job_problems),
            red=mutants(
                CI,
                a_job_keyed_on_push=lambda wf: jobs(wf)["pytest"].update(
                    {"if": "github.event_name == 'push'"}
                ),
                a_job_gone_from_the_census=lambda wf: jobs(wf).pop("hooks"),
                a_job_that_skips_a_dispatch=lambda wf: jobs(wf)[
                    "renovate-config"
                ].update({"if": "github.event_name == 'pull_request'"}),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-dispatch-takes-the-full-sweep-not-the-scoped-path",
            reason=(
                "Both scoping conditions key on `pull_request`, so a dispatch falls through to "
                "the full-sweep arm. Rewriting one as `github.event_name == 'push'` leaves a "
                "dispatch fast, green and checking nothing, and nothing in the file looks "
                "wrong (#2366). The scoped path diffs against `github.base_ref`, which is "
                "empty off a pull request. A step renamed out of the census is unchecked, so "
                "the census fails when it loses a name."
            ),
            offence=judge(_dispatch_step_problems),
            red=mutants(
                CI,
                a_sweep_step_keyed_on_push=_set_if(
                    FULL_SWEEP, "github.event_name == 'push'"
                ),
                a_sweep_step_keyed_on_push_or_a_step_output=_set_if(
                    FULL_SWEEP,
                    "github.event_name == 'push' || steps.changed.outputs.mode == 'full'",
                ),
                a_scoped_step_with_no_condition=_set_if(REJECT, None),
                a_scoped_step_keyed_on_not_a_push=_set_if(
                    "Render-diff gates", "github.event_name != 'push'"
                ),
                a_scoped_step_that_skips_pull_requests=_set_if(
                    REJECT, "github.event_name == 'push'"
                ),
                a_renamed_sweep_step=_rename(FULL_SWEEP, "Run hooks"),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-dispatch-has-its-own-concurrency-group",
            reason=(
                "A group with no dispatch arm falls through to the `github.sha` arm and joins "
                "the push group of the SHA the dispatch ran on. GitHub keeps one pending run "
                "per group, so the dispatch can evict that SHA's queued push run and leave it "
                "with no verdict. `test_ci_master_runs_are_not_evictable.py` owns the push arm "
                "of the same group."
            ),
            offence=judge(_dispatch_group_problems),
            red=mutants(
                CI,
                a_group_without_a_dispatch_arm=lambda wf: wf["concurrency"].update(
                    group=NO_DISPATCH_ARM
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="host-package-scans-run-on-every-pr",
            reason=(
                "`host_packages_build`'s later steps stand down when no rendered tree differs. "
                "The gitleaks scan and the placeholder check read the head render's package "
                "roots, and a PR that changes only `.gitleaks.toml` or the gitleaks pin renders "
                "nothing differently, so a scan behind that gate skips on exactly the PRs that "
                "change what it enforces (H4.1c). Each scan sits after the render step, carries "
                "no `if:`, and comes before the first step that tests the render's `changed` "
                "output."
            ),
            offence=judge(_scan_problems),
            red=mutants(
                CI,
                a_gated_scan=_edit_step(FIRST_SCAN, {"if": f"{GATE} == 'true'"}),
                a_scan_after_the_gate=_move_scan(FIRST_SCAN, None),
                a_scan_before_the_render=_move_scan(SECOND_SCAN, 0),
                a_missing_scan_step=_rename(FIRST_SCAN, "Scan"),
                a_scan_that_runs_another_command=_edit_step(
                    SECOND_SCAN, {"run": "true"}
                ),
            ),
        )
    ),
)
