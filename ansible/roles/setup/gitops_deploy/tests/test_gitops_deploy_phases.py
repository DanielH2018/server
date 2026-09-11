"""Each phase of a tick, driven on its own.

`main()` was one 545-line function, so every branch of it could only be reached by running the
whole tick: the fetch, the CI verdict, the classification and the deploy dispatch all had to be
scripted to assert anything about the last of them. It is now `assess()` -> `plan_tick()` ->
one `handle_*` per terminal branch, and this module calls each of those directly.

`test_gitops_deploy_main_branches.py` still drives whole ticks and is where the end-to-end
orderings are asserted (hold before reset, staging before merge). This file is the other half:
one phase, one input, one verdict. The `tick` fixture supplies the scripted checkout for both.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_phases.py
"""

import dataclasses

import pytest
import deploy_handlers
import deploy_phases
import deploy_tick_types
from deploy_changes import ChangeSet

LOCAL = "1" * 40
ORIGIN = "2" * 40
# Two commits between LOCAL and the tip, newest first — what the ancestor walk chooses from.
NEWER_GREEN = "3" * 40
OLDER_GREEN = "4" * 40


def _plan(gitops_deploy, cs: ChangeSet, paths=None, k8s_services=None):
    return deploy_tick_types.TickPlan(
        cs=cs, paths=list(paths or []), k8s_services=set(k8s_services or ())
    )


def _target(gitops_deploy, **overrides):
    """A TickTarget with the ordinary-push defaults, minus whatever the caller overrides.

    `dataclasses.replace` rather than a dict of kwargs: a dict merged with `**overrides` widens
    every field's type to the union of all of them, which `ty` rejects at the constructor.
    """
    return dataclasses.replace(
        deploy_tick_types.TickTarget(
            local=LOCAL,
            origin=ORIGIN,
            hold=None,
            dirty=False,
            status="",
            action="deploy",
        ),
        **overrides,
    )


# ── assess() ──────────────────────────────────────────────────────────────────────────────
def test_assess_reads_both_heads_and_classifies_an_ordinary_push(
    gitops_deploy, tick, settings
):
    tick.paths = ["ansible/roles/containers/sonarr/templates/docker-compose.yml.j2"]
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert (target.local, target.origin) == (LOCAL, ORIGIN)
    assert target.action == "deploy" and target.dirty is False


def test_assess_reports_a_dirty_tree_without_fetching_a_ci_verdict(
    gitops_deploy, tick, settings
):
    """The CI call is spent only on a tick that would otherwise deploy — one request per tick
    is the whole of this deployer's share of the GitHub rate limit."""
    tick.dirty = True
    tick.ci = "fail"  # would change the action if it were consulted
    assert (
        deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings).action
        == "dirty"
    )


def test_assess_fast_forwards_to_the_newest_green_ancestor_of_a_pending_tip(
    gitops_deploy, tick, settings, capsys
):
    """The tip is pending on most ticks that would deploy (124 merges/day, ~103s sweep).

    The chosen SHA becomes `target.origin`, and the REAL tip stays on `target.tip` so the
    behind-origin watchdog still has a tip to name.
    """
    tick.ci = "pending"
    tick.rev_list = [ORIGIN, NEWER_GREEN, OLDER_GREEN]
    tick.ancestor_ci = {NEWER_GREEN: "pass", OLDER_GREEN: "pass"}
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert (target.origin, target.action) == (NEWER_GREEN, "deploy")
    assert (target.tip, target.tip_ci) == (ORIGIN, "pending")
    assert (
        f"origin {ORIGIN[:8]}: CI pending; fast-forwarding to the newest green ancestor "
        f"{NEWER_GREEN[:8]} (1 behind the tip)" in capsys.readouterr().out
    )


def test_assess_still_defers_when_no_ancestor_in_the_walk_is_green(
    gitops_deploy, tick, settings, capsys
):
    """The rejecting half: an all-red walk leaves the tip's own verdict deciding the tick.

    It says how many commits it read, because a walk that found nothing and a tip with
    nothing below it both defer, and only the line tells an operator which one happened.
    """
    tick.ci = "pending"
    tick.rev_list = [ORIGIN, NEWER_GREEN]
    tick.ancestor_ci = {NEWER_GREEN: "fail"}
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert (target.origin, target.action) == (ORIGIN, "ci_pending")
    assert "no green ancestor in the 1 commit(s)" in capsys.readouterr().out


def test_an_unauthenticated_host_does_not_walk_at_all(
    gitops_deploy, tick, settings, capsys
):
    """Anonymous, the whole host shares 60 GitHub requests an hour; a walk spends ten.

    Exhausting it reads as "CI not finished" for every reader on the host, so the walk would
    buy one tick's latency by deferring the next several. Nothing below the tip is scripted
    here, so the fake raises if the walk spends a single request.
    """
    tick.ci = "pending"
    tick.authenticated = False
    tick.rev_list = [ORIGIN, NEWER_GREEN]
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert (target.origin, target.action) == (ORIGIN, "ci_pending")
    assert not [argv for argv in tick.git if argv[1] == "rev-list"]
    assert "no GitHub token" in capsys.readouterr().out


def test_assess_skips_a_red_ancestor_and_takes_the_green_one_below_it(
    gitops_deploy, tick, settings
):
    """A red ancestor is skipped, never chosen — the walk stops at the first PASS."""
    tick.ci = "fail"
    tick.rev_list = [ORIGIN, NEWER_GREEN, OLDER_GREEN]
    tick.ancestor_ci = {NEWER_GREEN: "fail", OLDER_GREEN: "pass"}
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert (target.origin, target.action) == (OLDER_GREEN, "deploy")
    assert target.red_tip == ORIGIN


def test_the_walk_stops_at_the_configured_maximum(gitops_deploy, tick, settings):
    """The bound is on GitHub requests per tick, and the tip's own verdict is one of them."""
    tick.ci = "pending"
    tick.rev_list = [ORIGIN, NEWER_GREEN, OLDER_GREEN]
    tick.ancestor_ci = {NEWER_GREEN: "fail", OLDER_GREEN: "pass"}
    capped = dataclasses.replace(settings, ci_ancestor_walk_max=2)
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, capped)
    assert (target.origin, target.action) == (ORIGIN, "ci_pending")


def test_assess_never_chooses_the_held_sha_as_an_ancestor(
    gitops_deploy, tick, state_dir, settings
):
    """A SHA a previous deploy failed on is not a tree to converge on, tip or ancestor."""
    (state_dir / "hold_sha").write_text(NEWER_GREEN)
    tick.ci = "pending"
    tick.rev_list = [ORIGIN, NEWER_GREEN, OLDER_GREEN]
    tick.ancestor_ci = {NEWER_GREEN: "pass", OLDER_GREEN: "pass"}
    target = deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert (target.origin, target.action) == (OLDER_GREEN, "deploy")


def test_a_green_tip_never_lists_the_commits_below_it(gitops_deploy, tick, settings):
    """The walk runs only on a tick that would otherwise defer — no extra git, no extra API."""
    tick.paths = ["docs/runbook.md"]
    deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert not [argv for argv in tick.git if argv[1] == "rev-list"]


def test_assess_records_a_divergence_and_clears_it_on_the_next_tick(
    gitops_deploy, tick, state_dir, settings
):
    tick.origin_ahead = False
    tick.local_ahead = False
    deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert gitops_deploy.STATE.diverged_sha == ORIGIN
    tick.origin_ahead = True
    deploy_phases.assess(tick.tools, gitops_deploy.STATE, settings)
    assert gitops_deploy.STATE.diverged_sha is None


def test_assess_raises_retryable_on_a_git_failure(gitops_deploy, tick, settings):
    """A transient tree state must skip the tick, not page — entrypoint() owns that contract."""
    import dataclasses
    import subprocess

    def broken(_repo):
        return subprocess.CompletedProcess(
            ["git", "status"], 128, stdout="", stderr="fatal: not a work tree"
        )

    tools = dataclasses.replace(tick.tools, git_status=broken)
    with pytest.raises(gitops_deploy.RetryableFetchError, match="not a work tree"):
        deploy_phases.assess(tools, gitops_deploy.STATE, settings)


# ── plan_tick() ───────────────────────────────────────────────────────────────────────────
def test_plan_tick_maps_a_template_push_to_its_service(gitops_deploy, tick, settings):
    tick.paths = ["ansible/roles/containers/sonarr/templates/docker-compose.yml.j2"]
    plan = deploy_phases.plan_tick(
        tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy)
    )
    assert plan.cs.services == {"sonarr"}
    assert plan.paths == tick.paths


def test_plan_tick_reroutes_a_service_this_host_runs_under_k8s(
    gitops_deploy, tick, settings
):
    """A containers/ path maps to <svc> by name alone and cannot see the platform; the host's
    own containers_list is what decides."""
    tick.declare("containers_list:\n  - name: wg-easy\n    platform: k8s\n")
    tick.paths = ["ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"]
    plan = deploy_phases.plan_tick(
        tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy)
    )
    assert plan.cs.services == set()
    assert "wg-easy" in plan.cs.k8s
    assert plan.k8s_services == {"wg-easy"}


def test_plan_tick_drops_a_comment_only_change_to_a_bring_up_playbook(
    gitops_deploy, tick, capsys, settings
):
    """Parking on a comment cost three sessions their landings on 2026-09-02 (PR #746)."""
    tick.paths = ["ansible/bootstrap.yml"]
    tick.files = {
        f"{LOCAL}:ansible/bootstrap.yml": "# old comment\n- hosts: all\n",
        f"{ORIGIN}:ansible/bootstrap.yml": "# new comment\n- hosts: all\n",
    }
    plan = deploy_phases.plan_tick(
        tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy)
    )
    assert plan.paths == []
    assert not plan.cs.broad_manual
    assert "not parking" in capsys.readouterr().out


# ── handle_dirty() ────────────────────────────────────────────────────────────────────────
def test_handle_dirty_logs_the_paths_on_every_tick(
    gitops_deploy, tick, state_dir, capsys, settings
):
    """Unthrottled, unlike the Discord page: an empty journal reads exactly like a tick with
    nothing to do, which is most of what the 2026-08-30 40-minute park cost."""
    target = _target(gitops_deploy, dirty=True, action="dirty", status=" M some/file\n")
    assert (
        deploy_handlers.handle_dirty(tick.tools, gitops_deploy.STATE, settings, target)
        == 0
    )
    assert "working tree dirty" in capsys.readouterr().out


def test_handle_dirty_pages_at_most_once_per_slot(
    gitops_deploy, tick, state_dir, settings
):
    target = _target(gitops_deploy, dirty=True, action="dirty", status=" M some/file\n")
    deploy_handlers.handle_dirty(tick.tools, gitops_deploy.STATE, settings, target)
    first = len(tick.posts)
    deploy_handlers.handle_dirty(tick.tools, gitops_deploy.STATE, settings, target)
    assert len(tick.posts) == first, "a second tick in the same slot must not re-page"


# ── handle_ci_failed() ────────────────────────────────────────────────────────────────────
def test_handle_ci_failed_pages_once_per_sha_and_deploys_nothing(
    gitops_deploy, tick, state_dir, settings
):
    target = _target(gitops_deploy, action="ci_failed")
    assert (
        deploy_handlers.handle_ci_failed(
            tick.tools, gitops_deploy.STATE, settings, target
        )
        == 0
    )
    deploy_handlers.handle_ci_failed(tick.tools, gitops_deploy.STATE, settings, target)
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert tick.playbooks == [] and tick.merges == []


# ── handle_broad() ────────────────────────────────────────────────────────────────────────
def test_handle_broad_defers_a_bring_up_playbook_without_merging(
    gitops_deploy, tick, state_dir, settings
):
    """Staying parked is what keeps `behind_since` set, the only durable signal that a plane is
    unapplied."""
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_manual=True),
        paths=["ansible/bootstrap.yml"],
    )
    assert (
        deploy_handlers.handle_broad(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert tick.merges == [], "the manual arm must not fast-forward"
    assert tick.playbooks == []
    assert "broad change needing a hand" in tick.posts[0]


def test_handle_broad_merges_before_it_applies_the_setup_plane(
    gitops_deploy, tick, state_dir, settings
):
    """Ansible renders from the working tree, so applying first deploys the pre-merge files and
    recaps changed=0 — indistinguishable from a clean idempotent run."""
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    assert (
        deploy_handlers.handle_broad(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert tick.merges == [ORIGIN]
    assert tick.index("git", "merge") < tick.index("playbook", "ansible-playbook")
    assert tick.playbooks[0][-2:] == ["--tags", "gitops_deploy"]


def test_a_successful_broad_apply_records_what_it_applied(
    gitops_deploy, tick, state_dir, settings
):
    """The evidence `land.sh` needs to tell an applied plane from one it fast-forwarded past.

    `behind_since` empty says only local == origin, which any session's `git merge --ff-only`
    produces too — PR #1529 landed `settled` over a plane four days stale on disk (#1537).
    """
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    deploy_handlers.handle_broad(
        tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
    )
    assert gitops_deploy.STATE.broad_applied == (
        f"{ORIGIN} ansible/initial_setup.yml gitops_deploy"
    )


def test_a_failed_broad_apply_records_no_apply(
    gitops_deploy, tick, state_dir, settings
):
    """The must-not-fire half: an attempted apply is not an apply, so the marker stays absent
    and `land.sh` keeps saying the plane is unfinished."""
    tick.playbook_outcomes = [RuntimeError("uv run ansible-playbook -> 2\nboom")]
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    deploy_handlers.handle_broad(
        tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
    )
    assert gitops_deploy.STATE.broad_applied is None


def test_a_failed_broad_apply_holds_the_plane_and_does_not_reset(
    gitops_deploy, tick, state_dir, settings
):
    """Forward-only: resetting without redeploying would leave the tree claiming the old commit
    while live state is half-new."""
    tick.playbook_outcomes = [RuntimeError("uv run ansible-playbook -> 2\nboom")]
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    assert (
        deploy_handlers.handle_broad(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert gitops_deploy.STATE.hold_sha == ORIGIN
    assert gitops_deploy.STATE.hold_plane
    assert not [argv for argv in tick.git if argv[1] == "reset"]
    assert "broad apply failed" in tick.posts[-1]


# ── handle_k8s() ──────────────────────────────────────────────────────────────────────────
def test_handle_k8s_gates_then_merges_then_deploys(
    gitops_deploy, tick, state_dir, settings
):
    plan = _plan(gitops_deploy, ChangeSet(k8s_deploy={"sonarr"}))
    assert (
        deploy_handlers.handle_k8s(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert tick.index("staging", "sonarr") < tick.index("git", "merge")
    assert tick.merges == [ORIGIN]
    assert tick.playbooks[0][-2:] == ["--tags", "sonarr"]
    assert ("annotation", {"sonarr"}) in tick.log


def test_handle_k8s_rolls_back_to_the_failed_shas_snapshot(
    gitops_deploy, tick, state_dir, settings
):
    """`origin[:8]`, never `local`: the snapshot worth reverting to is the one taken before the
    deploy that failed."""
    tick.playbook_outcomes = [
        RuntimeError("uv run ansible-playbook -> 2\nrollout failed")
    ]
    plan = _plan(gitops_deploy, ChangeSet(k8s_deploy={"sonarr"}))
    deploy_handlers.handle_k8s(
        tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
    )
    assert gitops_deploy.STATE.hold_sha == ORIGIN
    rollback = tick.playbooks[-1]
    assert f"k8s_restore_snapshot_sha={ORIGIN[:8]}" in rollback
    assert ("annotation", {"sonarr"}) not in tick.log


# ── handle_no_services() ──────────────────────────────────────────────────────────────────
def test_handle_no_services_merges_and_flags_a_rotated_secret(
    gitops_deploy, tick, state_dir, settings
):
    plan = _plan(
        gitops_deploy, ChangeSet(secrets=True), paths=["ansible/vars/secrets.yml"]
    )
    assert (
        deploy_handlers.handle_no_services(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == []
    assert "changed in" in tick.posts[0]


# ── handle_docker() ───────────────────────────────────────────────────────────────────────
def test_handle_docker_merges_deploys_and_clears_the_hold(
    gitops_deploy, tick, state_dir, settings
):
    gitops_deploy.STATE.write("hold", "0" * 40)
    tick.render("sonarr")
    plan = _plan(gitops_deploy, ChangeSet(services={"sonarr"}))
    assert (
        deploy_handlers.handle_docker(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert tick.merges == [ORIGIN]
    assert gitops_deploy.STATE.hold_sha is None


def test_handle_docker_holds_before_it_resets_when_the_gate_fails(
    gitops_deploy, tick, state_dir, settings
):
    """A hung rollback redeploy is SIGTERMed at TimeoutStartSec, so a hold written afterwards is
    a hold that never lands and a bad commit that redeploys every tick."""
    tick.render("sonarr")
    tick.healthy["sonarr"] = False
    plan = _plan(gitops_deploy, ChangeSet(services={"sonarr"}))
    assert (
        deploy_handlers.handle_docker(
            tick.tools, gitops_deploy.STATE, settings, _target(gitops_deploy), plan
        )
        == 0
    )
    assert gitops_deploy.STATE.hold_sha == ORIGIN
    assert [argv for argv in tick.git if argv[1] == "reset"]
    assert "rollback" in tick.posts[-1]
