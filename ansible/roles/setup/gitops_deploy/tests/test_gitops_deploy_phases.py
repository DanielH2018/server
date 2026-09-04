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

import pytest
from deploy_changes import ChangeSet

LOCAL = "1" * 40
ORIGIN = "2" * 40


def _plan(gitops_deploy, cs: ChangeSet, paths=None, k8s_services=None):
    return gitops_deploy.TickPlan(
        cs=cs, paths=list(paths or []), k8s_services=set(k8s_services or ())
    )


def _target(gitops_deploy, **overrides):
    fields = {
        "local": LOCAL,
        "origin": ORIGIN,
        "hold": None,
        "dirty": False,
        "status": "",
        "action": "deploy",
    }
    return gitops_deploy.TickTarget(**(fields | overrides))


# ── assess() ──────────────────────────────────────────────────────────────────────────────
def test_assess_reads_both_heads_and_classifies_an_ordinary_push(gitops_deploy, tick):
    tick.paths = ["ansible/roles/containers/sonarr/templates/docker-compose.yml.j2"]
    target = gitops_deploy.assess()
    assert (target.local, target.origin) == (LOCAL, ORIGIN)
    assert target.action == "deploy" and target.dirty is False


def test_assess_reports_a_dirty_tree_without_fetching_a_ci_verdict(gitops_deploy, tick):
    """The CI call is spent only on a tick that would otherwise deploy — one request per tick
    is the whole of this deployer's share of the GitHub rate limit."""
    tick.dirty = True
    tick.ci = "fail"  # would change the action if it were consulted
    assert gitops_deploy.assess().action == "dirty"


def test_assess_records_a_divergence_and_clears_it_on_the_next_tick(
    gitops_deploy, tick, state_dir
):
    tick.origin_ahead = False
    tick.local_ahead = False
    gitops_deploy.assess()
    assert gitops_deploy.STATE.diverged_sha == ORIGIN
    tick.origin_ahead = True
    gitops_deploy.assess()
    assert gitops_deploy.STATE.diverged_sha is None


def test_assess_raises_retryable_on_a_git_failure(gitops_deploy, tick, monkeypatch):
    """A transient tree state must skip the tick, not page — entrypoint() owns that contract."""
    import subprocess

    import deploy_io

    def broken(argv, **kwargs):
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(
                argv, 128, stdout="", stderr="fatal: not a work tree"
            )
        return tick.subprocess_run(argv, **kwargs)

    monkeypatch.setattr(
        deploy_io, "git_status", lambda _repo: broken(["git", "status"])
    )
    with pytest.raises(gitops_deploy.RetryableFetchError, match="not a work tree"):
        gitops_deploy.assess()


# ── plan_tick() ───────────────────────────────────────────────────────────────────────────
def test_plan_tick_maps_a_template_push_to_its_service(gitops_deploy, tick):
    tick.paths = ["ansible/roles/containers/sonarr/templates/docker-compose.yml.j2"]
    plan = gitops_deploy.plan_tick(_target(gitops_deploy))
    assert plan.cs.services == {"sonarr"}
    assert plan.paths == tick.paths


def test_plan_tick_reroutes_a_service_this_host_runs_under_k8s(gitops_deploy, tick):
    """A containers/ path maps to <svc> by name alone and cannot see the platform; the host's
    own containers_list is what decides."""
    tick.declare("containers_list:\n  - name: wg-easy\n    platform: k8s\n")
    tick.paths = ["ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"]
    plan = gitops_deploy.plan_tick(_target(gitops_deploy))
    assert plan.cs.services == set()
    assert "wg-easy" in plan.cs.k8s
    assert plan.k8s_services == {"wg-easy"}


def test_plan_tick_drops_a_comment_only_change_to_a_bring_up_playbook(
    gitops_deploy, tick, capsys
):
    """Parking on a comment cost three sessions their landings on 2026-09-02 (PR #746)."""
    tick.paths = ["ansible/bootstrap.yml"]
    tick.files = {
        f"{LOCAL}:ansible/bootstrap.yml": "# old comment\n- hosts: all\n",
        f"{ORIGIN}:ansible/bootstrap.yml": "# new comment\n- hosts: all\n",
    }
    plan = gitops_deploy.plan_tick(_target(gitops_deploy))
    assert plan.paths == []
    assert not plan.cs.broad_manual
    assert "not parking" in capsys.readouterr().out


# ── handle_dirty() ────────────────────────────────────────────────────────────────────────
def test_handle_dirty_logs_the_paths_on_every_tick(
    gitops_deploy, tick, state_dir, capsys
):
    """Unthrottled, unlike the Discord page: an empty journal reads exactly like a tick with
    nothing to do, which is most of what the 2026-08-30 40-minute park cost."""
    target = _target(gitops_deploy, dirty=True, action="dirty", status=" M some/file\n")
    assert gitops_deploy.handle_dirty(target) == 0
    assert "working tree dirty" in capsys.readouterr().out


def test_handle_dirty_pages_at_most_once_per_slot(gitops_deploy, tick, state_dir):
    target = _target(gitops_deploy, dirty=True, action="dirty", status=" M some/file\n")
    gitops_deploy.handle_dirty(target)
    first = len(tick.posts)
    gitops_deploy.handle_dirty(target)
    assert len(tick.posts) == first, "a second tick in the same slot must not re-page"


# ── handle_ci_failed() ────────────────────────────────────────────────────────────────────
def test_handle_ci_failed_pages_once_per_sha_and_deploys_nothing(
    gitops_deploy, tick, state_dir
):
    target = _target(gitops_deploy, action="ci_failed")
    assert gitops_deploy.handle_ci_failed(target) == 0
    gitops_deploy.handle_ci_failed(target)
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert tick.playbooks == [] and tick.merges == []


# ── handle_broad() ────────────────────────────────────────────────────────────────────────
def test_handle_broad_defers_a_bring_up_playbook_without_merging(
    gitops_deploy, tick, state_dir
):
    """Staying parked is what keeps `behind_since` set, the only durable signal that a plane is
    unapplied."""
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_manual=True),
        paths=["ansible/bootstrap.yml"],
    )
    assert gitops_deploy.handle_broad(_target(gitops_deploy), plan) == 0
    assert tick.merges == [], "the manual arm must not fast-forward"
    assert tick.playbooks == []
    assert "broad change needing a hand" in tick.posts[0]


def test_handle_broad_merges_before_it_applies_the_setup_plane(
    gitops_deploy, tick, state_dir
):
    """Ansible renders from the working tree, so applying first deploys the pre-merge files and
    recaps changed=0 — indistinguishable from a clean idempotent run."""
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    assert gitops_deploy.handle_broad(_target(gitops_deploy), plan) == 0
    assert tick.merges == [ORIGIN]
    assert tick.index("git", "merge") < tick.index("playbook", "ansible-playbook")
    assert tick.playbooks[0][-2:] == ["--tags", "gitops_deploy"]


def test_a_failed_broad_apply_holds_the_plane_and_does_not_reset(
    gitops_deploy, tick, state_dir
):
    """Forward-only: resetting without redeploying would leave the tree claiming the old commit
    while live state is half-new."""
    tick.playbook_outcomes = [RuntimeError("uv run ansible-playbook -> 2\nboom")]
    plan = _plan(
        gitops_deploy,
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    assert gitops_deploy.handle_broad(_target(gitops_deploy), plan) == 0
    assert gitops_deploy.STATE.hold_sha == ORIGIN
    assert gitops_deploy.STATE.hold_plane
    assert not [argv for argv in tick.git if argv[1] == "reset"]
    assert "broad apply failed" in tick.posts[-1]


# ── handle_k8s() ──────────────────────────────────────────────────────────────────────────
def test_handle_k8s_gates_then_merges_then_deploys(gitops_deploy, tick, state_dir):
    plan = _plan(gitops_deploy, ChangeSet(k8s_deploy={"sonarr"}))
    assert gitops_deploy.handle_k8s(_target(gitops_deploy), plan) == 0
    assert tick.index("staging", "sonarr") < tick.index("git", "merge")
    assert tick.merges == [ORIGIN]
    assert tick.playbooks[0][-2:] == ["--tags", "sonarr"]
    assert ("annotation", {"sonarr"}) in tick.log


def test_handle_k8s_rolls_back_to_the_failed_shas_snapshot(
    gitops_deploy, tick, state_dir
):
    """`origin[:8]`, never `local`: the snapshot worth reverting to is the one taken before the
    deploy that failed."""
    tick.playbook_outcomes = [
        RuntimeError("uv run ansible-playbook -> 2\nrollout failed")
    ]
    plan = _plan(gitops_deploy, ChangeSet(k8s_deploy={"sonarr"}))
    gitops_deploy.handle_k8s(_target(gitops_deploy), plan)
    assert gitops_deploy.STATE.hold_sha == ORIGIN
    rollback = tick.playbooks[-1]
    assert f"k8s_restore_snapshot_sha={ORIGIN[:8]}" in rollback
    assert ("annotation", {"sonarr"}) not in tick.log


# ── handle_no_services() ──────────────────────────────────────────────────────────────────
def test_handle_no_services_merges_and_flags_a_rotated_secret(
    gitops_deploy, tick, state_dir
):
    plan = _plan(
        gitops_deploy, ChangeSet(secrets=True), paths=["ansible/vars/secrets.yml"]
    )
    assert gitops_deploy.handle_no_services(_target(gitops_deploy), plan) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == []
    assert "changed in" in tick.posts[0]


# ── handle_docker() ───────────────────────────────────────────────────────────────────────
def test_handle_docker_merges_deploys_and_clears_the_hold(
    gitops_deploy, tick, state_dir
):
    gitops_deploy.STATE.write("hold", "0" * 40)
    tick.render("sonarr")
    plan = _plan(gitops_deploy, ChangeSet(services={"sonarr"}))
    assert gitops_deploy.handle_docker(_target(gitops_deploy), plan) == 0
    assert tick.merges == [ORIGIN]
    assert gitops_deploy.STATE.hold_sha is None


def test_handle_docker_holds_before_it_resets_when_the_gate_fails(
    gitops_deploy, tick, state_dir
):
    """A hung rollback redeploy is SIGTERMed at TimeoutStartSec, so a hold written afterwards is
    a hold that never lands and a bad commit that redeploys every tick."""
    tick.render("sonarr")
    tick.healthy = False
    plan = _plan(gitops_deploy, ChangeSet(services={"sonarr"}))
    assert gitops_deploy.handle_docker(_target(gitops_deploy), plan) == 0
    assert gitops_deploy.STATE.hold_sha == ORIGIN
    assert [argv for argv in tick.git if argv[1] == "reset"]
    assert "rollback" in tick.posts[-1]
