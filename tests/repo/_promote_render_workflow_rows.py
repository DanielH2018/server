"""The promote workflow's render bundle, its window-end schedule run and its uv environment.

Each row's red half is the real `promote.yml` with that one property broken. The other promote
row modules hold the workflow's guards, permissions and handoffs.
"""

from _promote_render_workflow import (
    AFTER_PROMOTE,
    CIPHERTEXT,
    SCHEDULED,
    SCHEDULED_PROMOTE,
    after_promote_problems,
    bundle_step_problems,
    CENSUS,
    history_problems,
    move_step_after,
    no_tag_branch_problems,
    render_schedule_problems,
    replace_in_build,
    replace_in_condition,
    reset_window_problems,
    shallow_promote,
    shared_only_in_the_tag_branch,
    shared_only_line_outside_the_no_tag_branch,
    stub_mode_problems,
    tag_branch_problems,
    upload_problems,
    uv_env_problems,
    uv_group_flag_problems,
    uv_override_problems,
    window_end_promote_problems,
    window_end_tag_problems,
)
from _promote_workflow import drop_steps, promote_row, step_by
from _row_table import Census
from _workflow_rows import PROMOTE, judge, mutants

PROMOTE_RENDER_ROWS = (
    Census(
        **promote_row(
            name="promote-render-censuses-the-bundle-before-gitleaks",
            reason=(
                "With no published tag the build step calls `cluster --shared-only "
                "--all-images`, because `cluster` without `--role` renders every tag. The "
                "census and gates step, `promote.py check-bundle`, runs after that build and "
                "before gitleaks, and `--against-prod` carries the tag rule."
            ),
            offence=judge(bundle_step_problems("check-bundle", CENSUS)),
            red=mutants(
                PROMOTE,
                the_census_after_gitleaks=move_step_after("check-bundle", "gitleaks"),
                no_census_step=drop_steps(
                    "render", lambda s: s.get("id") != "check-bundle"
                ),
                a_census_that_skips_the_tag_rule=lambda wf: step_by(
                    wf, "render", id="check-bundle"
                ).update(
                    run="uv run python scripts/deploy_tools/promote.py check-bundle out"
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-checks-sops-ciphertext-before-gitleaks",
            reason=(
                "`.gitleaks.toml` skips the bundle's SOPS copies, so `cluster_vars.py check "
                "--ciphertext-only` is the guard that stands in for the scan there. It runs "
                "after the build and before gitleaks."
            ),
            offence=judge(bundle_step_problems("ciphertext", CIPHERTEXT)),
            red=mutants(
                PROMOTE,
                the_ciphertext_check_after_gitleaks=move_step_after(
                    "ciphertext", "gitleaks"
                ),
                no_ciphertext_check=drop_steps(
                    "render", lambda s: s.get("id") != "ciphertext"
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-hands-out-after-its-checks",
            reason=(
                "The step that hands `out/` to `publish`, the job that pushes, runs after the "
                "census, the ciphertext check and gitleaks, and `publish` needs `render`."
            ),
            offence=judge(upload_problems),
            red=mutants(
                PROMOTE,
                gitleaks_after_the_handoff=move_step_after("gitleaks", None),
                a_publish_job_that_does_not_need_render=lambda wf: wf["jobs"][
                    "publish"
                ].pop("needs"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-builds-in-placeholder-mode",
            reason=(
                "The build passes no `--secret-stubs`, which is what selects placeholder mode "
                "(C1)."
            ),
            offence=judge(stub_mode_problems),
            red=mutants(
                PROMOTE,
                a_stub_mode_build=replace_in_build(
                    "--out out", "--out out --secret-stubs s.yml"
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-fetches-the-whole-history",
            reason=(
                "`decide` reads `Removes-Object` lines back to `prod`'s commit, so the promote "
                "job's one checkout fetches master's whole history."
            ),
            offence=judge(history_problems),
            red=mutants(PROMOTE, a_shallow_promote_checkout=shallow_promote),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-no-tag-branch-renders-the-shared-kinds",
            reason=(
                "With no published tag, `cluster` without `--role` renders every tag, so the "
                "no-tag branch runs `cluster --shared-only --all-images`. The bundle then holds "
                "the shared kinds, a root listing them (D3.3a) and every image context, which "
                "the `images` job refuses to run without. The check reads that branch alone: "
                "the same line in the tag branch or after the `if` block does not count."
            ),
            offence=judge(no_tag_branch_problems),
            red=mutants(
                PROMOTE,
                a_no_tag_branch_that_renders_no_shared_kind=replace_in_build(
                    " --shared-only", ""
                ),
                a_no_tag_branch_that_writes_no_image_context=replace_in_build(
                    " --shared-only --all-images", " --shared-only"
                ),
                shared_only_only_in_the_branch_that_publishes_tags=(
                    shared_only_in_the_tag_branch
                ),
                the_shared_only_line_in_the_else_branch=(
                    shared_only_line_outside_the_no_tag_branch("else")
                ),
                the_shared_only_line_after_the_if_block=(
                    shared_only_line_outside_the_no_tag_branch("after")
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-tag-branch-writes-every-image-context",
            reason=(
                "A `--role` render writes only the named tags' image contexts, and the "
                "`images` job builds every one from `out/images/`, so the tag branch passes "
                "`--all-images` too."
            ),
            offence=judge(tag_branch_problems),
            red=mutants(
                PROMOTE,
                a_tag_branch_that_renders_only_the_named_tags_images=replace_in_build(
                    " --all-images", ""
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-render-skips-the-scheduled-run",
            reason=(
                "The `schedule:` run at the reboot window's end promotes the commits merged "
                "during it, each already published by its own run. A `render` that runs on "
                "the schedule would publish an unchecked tip."
            ),
            offence=judge(render_schedule_problems),
            red=mutants(
                PROMOTE,
                render_on_the_schedule=lambda wf: wf["jobs"]["render"].update(
                    {"if": f"{SCHEDULED} || {wf['jobs']['render']['if']}"}
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-runs-on-the-scheduled-run-with-publish-skipped",
            reason=(
                "The scheduled run skips render, images and publish, so `promote` needs its "
                "own condition: it runs on a skipped publish for the scheduled run alone, "
                "under `!cancelled()`, which replaces the implicit success() that reads every "
                "ancestor. Without it the run decides and never moves `prod`, and CI stays "
                "green."
            ),
            offence=judge(window_end_promote_problems),
            red=mutants(
                PROMOTE,
                promote_on_implicit_success=replace_in_condition(
                    "promote", "!cancelled() &&", ""
                ),
                promote_on_a_published_tip_only=replace_in_condition(
                    "promote", SCHEDULED_PROMOTE, "false"
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-tag-reads-promotes-result",
            reason=(
                "The skipped publish is also `tag`'s ancestor, so `tag` uses `!cancelled()` "
                "and reads `needs.promote.result == 'success'` itself, or the scheduled run "
                "skips it."
            ),
            offence=judge(window_end_tag_problems),
            red=mutants(
                PROMOTE,
                tag_on_implicit_success=replace_in_condition(
                    "tag", "!cancelled() &&", ""
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-later-jobs-admit-the-scheduled-run",
            reason=(
                f"{', '.join(AFTER_PROMOTE)} each name the schedule in their `if`. A job that "
                "lacks it skips on the scheduled run, which decides and never moves `prod`."
            ),
            offence=judge(after_promote_problems),
            red=mutants(
                PROMOTE,
                tag_without_the_schedule=replace_in_condition(
                    "tag", f"{SCHEDULED} ||", ""
                ),
                settle_without_the_schedule=replace_in_condition(
                    "settle", f"{SCHEDULED} ||", ""
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-reset-jobs-need-no-window-job",
            reason=(
                "`decide` reads the reboot window in `promote`, and the jobs after it act on "
                "its verdict. A `reset_to_last_good` job that needed one of those would skip "
                "with the window, or wait on it."
            ),
            offence=judge(reset_window_problems),
            red=mutants(
                PROMOTE,
                a_reset_that_needs_promote=lambda wf: wf["jobs"]["reset-check"].update(
                    needs="promote"
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-env-installs-no-default-group",
            reason=(
                "The workflow env sets UV_NO_DEV and UV_NO_GROUP=lint, so an unreachable tool "
                "download in the `dev` or `lint` group (actionlint-py's build, #7625) cannot "
                "block a promotion."
            ),
            offence=judge(uv_env_problems),
            red=mutants(
                PROMOTE,
                the_dev_group_installed=lambda wf: wf["env"].pop("UV_NO_DEV"),
                the_lint_group_installed=lambda wf: wf["env"].pop("UV_NO_GROUP"),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-jobs-never-override-the-uv-env",
            reason=(
                "No job or step sets UV_NO_DEV or UV_NO_GROUP again, since the override "
                "reinstalls the group the workflow env drops."
            ),
            offence=judge(uv_override_problems),
            red=mutants(
                PROMOTE,
                render_re_enables_the_dev_group=lambda wf: (
                    wf["jobs"]["render"].setdefault("env", {}).update(UV_NO_DEV="")
                ),
            ),
        )
    ),
    Census(
        **promote_row(
            name="promote-uv-calls-ask-for-no-group",
            reason=(
                "A `uv run` or `uv sync` with `--dev`, `--only-dev`, `--group` or "
                "`--all-groups` installs a group the workflow env drops."
            ),
            offence=judge(uv_group_flag_problems),
            red=mutants(
                PROMOTE,
                a_step_asks_for_a_group=replace_in_build(
                    "uv run python", "uv run --group dev python"
                ),
            ),
        )
    ),
)
