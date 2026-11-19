"""The promote workflow keeps render code away from every token that can sign or move a tag.

`promote.yml` triggers on `workflow_run`, so GitHub runs master's copy of the file and a PR that
edits it never runs its own edit. Every property is therefore a row here, checked statically
before the edit reaches master. ADR-0020 records the decision behind each one. Each row's red
half is the real file with that one property broken.
"""

import copy

from _promote_workflow import (
    CONCURRENCY,
    COSIGN_RELEASE,
    JOBS,
    NEEDS,
    PERMISSIONS,
    REFUSE_JOB,
    REF_GUARD,
    RENDER_REF,
    RESET_GUARDS,
    RESET_SKIP,
    TRIGGER_GUARDS,
    VERDICT_GATE,
    add_checkout,
    ancestry_problems,
    checkout_problems,
    drop_steps,
    fetches,
    flux_version,
    guard_problems,
    handoff_problems,
    installed_in,
    job,
    move_build_before_ancestry,
    promote_row,
    remove_from,
    step_by,
    the_bot_on_the_starter_list,
    trigger_problems,
)
from _row_table import Census
from _workflow_rows import PROMOTE, checkouts, condition, judge, jobs, mutants, triggers

PROMOTE_ROWS = (
    Census(
        **promote_row(
            name="promote-runs-on-masters-ci-runs",
            reason=(
                "promote publishes and moves `prod`, so it starts only from a completed CI run "
                "on master. A `pull_request` trigger would run a PR author's edit of this file "
                "with its tokens."
            ),
            offence=judge(trigger_problems),
            red=mutants(
                PROMOTE,
                a_pull_request_trigger=lambda wf: triggers(wf).update(
                    pull_request=None
                ),
                a_trigger_on_every_branchs_CI=lambda wf: triggers(wf)[
                    "workflow_run"
                ].pop("branches"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-grants-no-workflow-permissions",
            reason=(
                "Each job states its own token scopes. A workflow-level grant reaches every "
                "job, the render jobs included."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if wf.get("permissions") == {}
                    else ["the workflow grants permissions above the job level"]
                )
            ),
            red=mutants(
                PROMOTE,
                a_workflow_level_grant=lambda wf: wf.update(
                    permissions={"contents": "read"}
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-jobs-are-the-known-thirteen",
            reason=(
                "Every row below names jobs by their ids. A job added or renamed runs with no "
                "row holding its guard, permissions or checkout until it joins JOBS."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if set(jobs(wf)) == set(JOBS)
                    else [f"the jobs are {sorted(jobs(wf))}, not {sorted(JOBS)}"]
                )
            ),
            red=mutants(
                PROMOTE,
                an_unlisted_job=lambda wf: wf["jobs"].update(
                    extra=copy.deepcopy(wf["jobs"]["post"])
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-jobs-carry-their-guards",
            reason=(
                "Every job runs only for a successful push run of CI on master, from master's "
                "copy of the file, started by an account on the starter list; the reset jobs "
                "only for a dispatch that sets reset_to_last_good. DanielClaudeBot holds write "
                "access, so without the starter guard it could dispatch a reset. "
                "`refuse-starter` runs on exactly the starter guard's negation."
            ),
            offence=judge(guard_problems),
            red=mutants(
                PROMOTE,
                a_trigger_guard_removed=remove_from("render", TRIGGER_GUARDS[2]),
                a_job_without_the_ref_check=remove_from("tag", REF_GUARD),
                the_bot_on_the_starter_list=the_bot_on_the_starter_list,
                a_refusal_that_is_not_the_guards_negation=lambda wf: wf["jobs"][
                    REFUSE_JOB
                ].update({"if": "github.event_name == 'workflow_dispatch'"}),
                reset_check_without_its_input=remove_from(
                    "reset-check", RESET_GUARDS[0]
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-jobs-hold-only-their-permissions",
            reason=(
                "Code that runs during a render or a decision never holds a token that can "
                "sign, push, move a tag or post a status. Each job's scopes are pinned exactly."
            ),
            offence=judge(
                lambda wf: [
                    f"{name}: permissions are {job(wf, name).get('permissions')}, not "
                    f"{wanted}"
                    for name, wanted in PERMISSIONS.items()
                    if job(wf, name).get("permissions") != wanted
                ]
            ),
            red=mutants(
                PROMOTE,
                an_extra_permission=lambda wf: wf["jobs"]["publish"][
                    "permissions"
                ].update(contents="read"),
                verify_holds_packages_write=lambda wf: wf["jobs"]["verify"][
                    "permissions"
                ].update(packages="write"),
                settle_with_id_token_write=lambda wf: wf["jobs"]["settle"][
                    "permissions"
                ].update({"id-token": "write"}),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-jobs-need-their-predecessors",
            reason=(
                "The `needs` chain is the order publish, promote, tag, verify, settle. A job "
                "that loses a `needs` starts before the output it reads exists."
            ),
            offence=judge(
                lambda wf: [
                    f"{name}: needs {job(wf, name).get('needs')!r}, not {wanted!r}"
                    for name, wanted in NEEDS.items()
                    if job(wf, name).get("needs") != wanted
                ]
            ),
            red=mutants(
                PROMOTE, tag_without_needs=lambda wf: wf["jobs"]["tag"].pop("needs")
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-moves-share-one-concurrency-group",
            reason=(
                "`promote` and `tag` share the `promote` group and the reset jobs the "
                "`promote-reset` group, none cancelling in progress, so two runs never move "
                "`prod` at once and a reset never waits behind a promotion."
            ),
            offence=judge(
                lambda wf: [
                    f"{name}: is not in the {group} concurrency group"
                    for name, group in CONCURRENCY.items()
                    if job(wf, name).get("concurrency")
                    != {"group": group, "cancel-in-progress": False}
                ]
            ),
            red=mutants(
                PROMOTE,
                tag_outside_the_promote_group=lambda wf: wf["jobs"]["tag"].pop(
                    "concurrency"
                ),
                a_reset_job_in_the_promote_group=lambda wf: wf["jobs"]["reset-move"][
                    "concurrency"
                ].update(group="promote"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-skips-a-reset",
            reason=(
                "A reset_to_last_good dispatch moves `prod` back and must render, publish and "
                "promote nothing, so `render`, which every promoting job needs, skips it."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if RESET_SKIP in condition(job(wf, "render"))
                    else [
                        f"render: its `if` lacks {RESET_SKIP}, so a reset also promotes"
                    ]
                )
            ),
            red=mutants(PROMOTE, render_on_a_reset=remove_from("render", RESET_SKIP)),
        )
    ),
    Census(
        **promote_row(
            name="promote-checkouts-name-their-ref",
            reason=(
                "`render` checks out the commit it publishes; the jobs holding a token check "
                "out the workflow's own commit, so a published commit's code never runs beside "
                "a write token. The jobs that sign, tag or settle check out nothing."
            ),
            offence=judge(checkout_problems),
            red=mutants(
                PROMOTE,
                promote_checks_out_head_sha=lambda wf: checkouts(wf["jobs"]["promote"])[
                    0
                ]["with"].update(ref=RENDER_REF),
                publish_checks_out=add_checkout("publish"),
                settle_checks_out_code=add_checkout("settle"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-checks-a-dispatched-commit-first",
            reason=(
                "A dispatch names any `sha`, so `render` refuses one that is not on master and "
                "re-reads its CI verdict, both before the build step runs its code."
            ),
            offence=judge(ancestry_problems),
            red=mutants(
                PROMOTE,
                render_without_the_ancestry_check=drop_steps(
                    "render", lambda s: s.get("id") != "ancestry"
                ),
                a_render_step_before_the_ancestry_step=move_build_before_ancestry,
                an_ancestry_step_that_reads_github_sha=lambda wf: step_by(
                    wf, "render", id="ancestry"
                )["env"].update(WANTED="${{ github.sha }}"),
                a_render_with_no_CI_recheck=drop_steps(
                    "render", lambda s: s.get("id") != "ci-recheck"
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-later-jobs-read-decides-outputs",
            reason=(
                "`decide` names the target and digest it verified. A step after it that reads "
                "the trigger's head_sha acts on a commit decide did not weigh."
            ),
            offence=judge(handoff_problems),
            red=mutants(
                PROMOTE,
                tag_reads_head_sha=lambda wf: step_by(wf, "tag", name="Move prod")[
                    "env"
                ].update(TARGET="${{ github.event.workflow_run.head_sha }}"),
                verify_waits_on_the_triggering_commit=lambda wf: step_by(
                    wf, "verify", id="verify"
                )["env"].update(TARGET="${{ github.event.workflow_run.head_sha }}"),
                lost_settle_not_handed_on=lambda wf: wf["jobs"]["promote"][
                    "outputs"
                ].pop("lost_settle"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-tag-waits-on-the-verdict",
            reason=(
                "Without the gate a `held` or `current` verdict reaches `tag`, which then fails "
                "red on the empty target instead of skipping, and the hold rests on a shell "
                "check alone."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if VERDICT_GATE in condition(job(wf, "tag"))
                    else [f"tag: its `if` lacks the verdict gate {VERDICT_GATE}"]
                )
            ),
            red=mutants(
                PROMOTE, tag_without_the_verdict_gate=remove_from("tag", VERDICT_GATE)
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-installs-flux-only-where-it-moves-tags",
            reason=(
                "The flux CLI pushes and tags the artifact, so only the jobs that do that "
                "fetch it."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if installed_in(wf, "flux")
                    == {"publish", "tag", "settle", "reconcile", "reset-move"}
                    else [
                        "the flux CLI is not installed in exactly publish, tag, settle, "
                        "reconcile and reset-move"
                    ]
                )
            ),
            red=mutants(
                PROMOTE,
                tag_without_its_flux_fetch=drop_steps(
                    "tag", lambda s: not fetches(s, "flux")
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-flux-cli-matches-the-controllers",
            reason=(
                "The CLI that pushes the artifact and the controllers that read it move "
                "together: FLUX_VERSION equals the flux role's `flux_version`. "
                "`scripts/tests/test_renovate_flux_cli.py` holds the other workflows' pins."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if (wf.get("env") or {}).get("FLUX_VERSION") == flux_version()
                    else [
                        f"FLUX_VERSION: flux CLI {(wf.get('env') or {}).get('FLUX_VERSION')!r} "
                        f"differs from flux_version {flux_version()!r}"
                    ]
                )
            ),
            red=mutants(
                PROMOTE,
                a_drifted_flux_CLI=lambda wf: wf["env"].update(FLUX_VERSION="v0.0.1"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-installs-cosign-only-where-it-signs",
            reason=(
                "cosign signs in `publish` and verifies in `promote`; no other job needs it."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if installed_in(wf, "cosign") == {"publish", "promote"}
                    else ["cosign is not installed in exactly publish and promote"]
                )
            ),
            red=mutants(
                PROMOTE,
                promote_without_its_cosign_fetch=drop_steps(
                    "promote", lambda s: not fetches(s, "cosign")
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-cosign-is-the-pinned-release",
            reason=(
                "The signature `publish` writes and the check `promote` makes come from one "
                "pinned cosign release."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if (wf.get("env") or {}).get("COSIGN_VERSION") == COSIGN_RELEASE
                    else [
                        f"COSIGN_VERSION: cosign {(wf.get('env') or {}).get('COSIGN_VERSION')!r} "
                        f"differs from {COSIGN_RELEASE!r}"
                    ]
                )
            ),
            red=mutants(
                PROMOTE,
                cosign_without_its_release=lambda wf: wf["env"].pop("COSIGN_VERSION"),
            ),
        )
    ),
)
