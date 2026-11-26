"""What the promote workflow's rows read: its jobs, guards and scopes, and the judges over them.

`_promote_workflow_rows.py` and the other promote row modules hold the rows. ADR-0020 records
the decision behind each property.
"""

import copy

from _row_table import Subject, tracked
from _workflow_rows import (
    PROMOTE,
    Workflow,
    checkouts,
    condition,
    jobs,
    load,
    step_index,
    steps,
    triggers,
)

FLUX_DEFAULTS = "k8s/roles/flux/defaults/main.yml"
# tool -> the release URL its inline fetch curls.
RELEASE_FETCH = {
    "flux": "fluxcd/flux2/releases/download/$FLUX_VERSION/",
    "cosign": "sigstore/cosign/releases/download/$COSIGN_VERSION/",
}
PINNED_TOOL = "$/.github/actions/pinned-tool"
COSIGN_RELEASE = "v3.1.4"

RESET_JOBS = ("reset-check", "reset-move")
# The job that fails a run started by an account off the starter list.
REFUSE_JOB = "refuse-starter"
JOBS = (
    "render",
    "images",
    "publish",
    "promote",
    "tag",
    "verify",
    "verdict-status",
    "settle",
    "post",
    "reconcile",
    *RESET_JOBS,
    REFUSE_JOB,
)
NEEDS = {
    "reset-move": "reset-check",
    "publish": ["render", "images"],
    "promote": "publish",
    "tag": "promote",
    "verify": ["promote", "tag"],
    "verdict-status": ["promote", "tag", "verify"],
    "settle": ["promote", "tag", "verify"],
    "post": ["promote", "tag", "verify", "settle"],
    "reconcile": "promote",
}

# Every job runs only for a successful push run of CI on master in this repository.
TRIGGER_GUARDS = (
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.head_branch == 'master'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
)
# DanielClaudeBot holds write access, so without this it could dispatch reset_to_last_good.
STARTER_GUARD = (
    "(github.event_name != 'workflow_dispatch' && github.run_attempt == '1' || "
    'contains(fromJSON(\'["DanielH2018", "github-actions[bot]"]\'), github.triggering_actor))'
)
# A dispatch reaches every job, so every job also requires master's copy of the file.
REF_GUARD = "github.ref == 'refs/heads/master'"
# The reset jobs run only for a dispatch of master's copy that sets the input.
RESET_GUARDS = (
    "inputs.reset_to_last_good",
    REF_GUARD,
    "github.event_name == 'workflow_dispatch'",
)
RESET_SKIP = "!inputs.reset_to_last_good"
PERMISSIONS = {
    "render": {"contents": "read", "checks": "read", "packages": "read"},
    "images": {"contents": "read", "packages": "write"},
    "publish": {"packages": "write", "id-token": "write"},
    "promote": {
        "contents": "read",
        "checks": "read",
        "packages": "read",
        "pull-requests": "read",
        "statuses": "read",
        "actions": "read",
    },
    "tag": {"packages": "write"},
    "verify": {"contents": "read", "statuses": "read", "packages": "read"},
    "verdict-status": {"statuses": "write"},
    "settle": {"packages": "write", "actions": "write"},
    "post": {"contents": "read"},
    "reconcile": {"packages": "write", "statuses": "read", "actions": "write"},
    "reset-check": {"contents": "read", "packages": "read", "actions": "read"},
    "reset-move": {"packages": "write"},
    REFUSE_JOB: {},
}
CONCURRENCY = {
    "promote": "promote",
    "tag": "promote",
    "reset-check": "promote-reset",
    "reset-move": "promote-reset",
}

RENDER_REF = "${{ inputs.sha || github.event.workflow_run.head_sha || github.sha }}"
PROMOTE_REF = "${{ github.sha }}"
# Each job that checks out, the ref it checks out, and whether it needs the full history.
# `verify` and `post` read no history: what they read came from `decide`. Nor does
# `reset-check`, which reads only the registry and DEPLOY_HOLD.
CHECKOUTS = {
    "render": (RENDER_REF, True),
    "promote": (PROMOTE_REF, True),
    "verify": (PROMOTE_REF, False),
    "post": (PROMOTE_REF, False),
    "reset-check": (PROMOTE_REF, False),
}
NO_CHECKOUT = ("publish", "tag", "verdict-status", "settle", "reconcile", "reset-move")
# The ancestry step must read the dispatched commit, never the workflow's own `github.sha`.
DISPATCHED_SHA = "inputs.sha"
HEAD_SHA = "workflow_run.head_sha"
# `tag` moves `prod` only when `decide` said so.
VERDICT_GATE = "needs.promote.outputs.verdict == 'promote'"
# decide's outputs that a later job's steps read, by the job.
HANDOFFS = {
    "target": "tag",
    "digest": "tag",
    "lost_settle": "reconcile",
    "lost_settle_digest": "reconcile",
}
AFTER_DECIDE = ("tag", "verify", "verdict-status", "settle", "post", "reconcile")


def promote_row(**rows) -> dict:
    """The arguments every promote row shares: its selector, floor and named member."""
    return {
        "files": lambda: tracked(PROMOTE),
        "must_find": frozenset({PROMOTE}),
        "green": (Subject(PROMOTE),),
        **rows,
    }


def job(wf: Workflow, name: str) -> dict:
    return jobs(wf).get(name, {})


def trigger_problems(wf: Workflow) -> list[str]:
    found = []
    run = triggers(wf).get("workflow_run") or {}
    if run.get("workflows") != ["CI"] or run.get("types") != ["completed"]:
        found.append("the workflow_run trigger is not CI's completed runs")
    if run.get("branches") != ["master"]:
        found.append("the workflow_run trigger is not filtered to master")
    for banned in ("pull_request", "pull_request_target"):
        if banned in triggers(wf):
            found.append(f"the workflow triggers on {banned}")
    return found


def guard_problems(wf: Workflow) -> list[str]:
    found = []
    for name in JOBS:
        cond = condition(job(wf, name))
        if name == REFUSE_JOB:
            if cond != f"!{STARTER_GUARD}":
                found.append(f"{name}: its `if` is not the negated starter guard")
            continue
        if STARTER_GUARD not in cond:
            found.append(f"{name}: its `if` lacks the starter guard")
            continue
        guards = RESET_GUARDS if name in RESET_JOBS else (*TRIGGER_GUARDS, REF_GUARD)
        found += [
            f"{name}: its `if` lacks the guard {guard}"
            for guard in guards
            if guard not in cond
        ]
    return found


def checkout_problems(wf: Workflow) -> list[str]:
    found = []
    for name, (ref, full_history) in CHECKOUTS.items():
        checked_out = checkouts(job(wf, name))
        if len(checked_out) != 1:
            found.append(f"{name}: has {len(checked_out)} checkout steps, not one")
            continue
        args = checked_out[0].get("with") or {}
        if args.get("ref") != ref:
            found.append(f"{name}: checks out {args.get('ref')!r}, not {ref!r}")
        if full_history and args.get("fetch-depth") != 0:
            found.append(f"{name}: checks out without fetch-depth: 0")
    found += [
        f"{name}: checks out the repository"
        for name in NO_CHECKOUT
        if checkouts(job(wf, name))
    ]
    return found


def ancestry_problems(wf: Workflow) -> list[str]:
    render = job(wf, "render")
    ancestry, build = step_index(render, "ancestry"), step_index(render, "build")
    recheck = step_index(render, "ci-recheck")
    if build is None:
        return ["render: has no step with id `build`"]
    if ancestry is None or "git merge-base --is-ancestor" not in str(
        steps(render)[ancestry].get("run", "")
    ):
        return [
            "render: runs no `git merge-base --is-ancestor` step with id `ancestry`"
        ]
    found = []
    if ancestry > build:
        found.append("render: checks ancestry after the build step")
    if DISPATCHED_SHA not in str(steps(render)[ancestry].get("env")):
        found.append(f"render: the ancestry step does not read {DISPATCHED_SHA}")
    if recheck is None or "promote.py ci-verdict" not in str(
        steps(render)[recheck].get("run", "")
    ):
        found.append(
            "render: runs no `promote.py ci-verdict` step with id `ci-recheck`"
        )
    elif recheck > build:
        found.append("render: re-checks a dispatched commit's CI after the build step")
    return found


def handoff_problems(wf: Workflow) -> list[str]:
    promote = job(wf, "promote")
    decide = step_index(promote, "decide")
    if decide is None:
        return ["promote: has no step with id `decide`"]
    found = []
    for key, reader in HANDOFFS.items():
        if (
            promote.get("outputs", {}).get(key)
            != f"${{{{ steps.decide.outputs.{key} }}}}"
        ):
            found.append(f"promote: output {key} does not come from the decide step")
        if f"needs.promote.outputs.{key}" not in str(job(wf, reader).get("steps")):
            found.append(f"{reader}: does not take {key} from promote's outputs")
    later = [job(wf, name) for name in AFTER_DECIDE]
    after_decide = steps(promote)[decide + 1 :] + [s for j in later for s in steps(j)]
    if any(HEAD_SHA in str(step) for step in after_decide) or any(
        HEAD_SHA in str(job.get("if", "")) for job in later
    ):
        found.append(f"a step after decide reads github.event.{HEAD_SHA}")
    return found


def fetches(step: dict, tool: str) -> bool:
    """Whether the step fetches `tool`'s pinned release, inline or through pinned-tool."""
    if step.get("uses") == PINNED_TOOL:
        return (step.get("with") or {}).get("tool") == tool
    return RELEASE_FETCH[tool] in str(step.get("run", ""))


def installed_in(wf: Workflow, tool: str) -> set[str]:
    return {
        name for name, job in jobs(wf).items() for s in steps(job) if fetches(s, tool)
    }


def flux_version() -> str:
    return load(FLUX_DEFAULTS)["flux_version"]


def remove_from(job: str, needle: str):
    def mutate(wf: Workflow) -> None:
        wf["jobs"][job]["if"] = wf["jobs"][job]["if"].replace(needle, "true")

    return mutate


def drop_steps(job: str, keep):
    def mutate(wf: Workflow) -> None:
        wf["jobs"][job]["steps"] = [s for s in wf["jobs"][job]["steps"] if keep(s)]

    return mutate


def add_checkout(job: str):
    def mutate(wf: Workflow) -> None:
        checkout = copy.deepcopy(checkouts(wf["jobs"]["render"])[0])
        wf["jobs"][job]["steps"].insert(0, checkout)

    return mutate


def the_bot_on_the_starter_list(wf: Workflow) -> None:
    for job in wf["jobs"].values():
        job["if"] = job["if"].replace(
            '"github-actions[bot]"]', '"github-actions[bot]", "DanielClaudeBot"]'
        )


def step_by(wf: Workflow, job: str, **match) -> dict:
    return next(
        s
        for s in wf["jobs"][job]["steps"]
        if all(s.get(k) == v for k, v in match.items())
    )


def move_build_before_ancestry(wf: Workflow) -> None:
    render = wf["jobs"]["render"]["steps"]
    build = step_by(wf, "render", id="build")
    render.remove(build)
    render.insert(1, build)
