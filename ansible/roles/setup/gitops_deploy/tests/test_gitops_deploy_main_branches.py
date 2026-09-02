"""main()'s branches, run against a scripted checkout.

Every invariant here used to be an AST guard on main()'s source: the ff-merge lands the SHA
the tick pinned, write_hold precedes every rollback reset, a rollback's exit code follows
whether its post was delivered, the diverged marker is written ahead of the action
branching, drain_pending() runs ahead of the short-circuits, the rollback redeploy passes the
FAILED commit's short SHA under its own budget, and a secrets change bundled with an image
bump is still flagged. The `tick` fixture in conftest.py answers git, ansible-playbook, the
CI verdict, the health gate, the staging gate and Discord from a script and records every
call in order, so each guard is now an assertion on what main() did.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_branches.py

import json

import pytest

# The SHAs the `tick` fixture starts from; `from conftest import` is avoided because the
# repo has several conftest.py files and the name resolves to whichever sys.path saw first.
LOCAL = "1" * 40
ORIGIN = "2" * 40
DOCKER_TEMPLATE = "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"
K8S_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"
DECLARES_WG_EASY = "containers_list:\n  - name: wg-easy\n    platform: docker\n"
DECLARES_SONARR = "containers_list:\n  - name: sonarr\n    platform: k8s\n"
DEPLOY_WG_EASY = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "wg-easy",
]


def _marker(state_dir, name: str) -> str | None:
    path = state_dir / name
    return path.read_text().strip() if path.exists() else None


# ── the short-circuits: nothing merges, nothing deploys ───────────────────────────────────────
def test_a_converged_checkout_is_a_noop(gitops_deploy, tick):
    tick.origin = tick.local
    assert gitops_deploy.main() == 0
    assert tick.merges == [] and tick.playbooks == [] and tick.posts == []


def test_drain_pending_runs_ahead_of_the_noop_short_circuit(
    gitops_deploy, tick, state_dir
):
    # The ff-merged channels never re-reach their alert code on a later tick, so a queued alert
    # is only recoverable at the top of EVERY tick, before local == origin returns.
    gitops_deploy._write_pending({"secrets:" + ORIGIN: "queued last tick"})
    tick.origin = tick.local
    gitops_deploy.main()
    assert tick.posts == ["queued last tick"]
    assert json.loads((state_dir / "pending_alerts.json").read_text()) == {}


def test_a_dirty_tree_skips_without_merging(gitops_deploy, tick):
    tick.dirty = True
    tick.paths = ["docs/x.md"]
    assert gitops_deploy.main() == 0
    assert tick.merges == [] and tick.playbooks == []


def test_a_held_sha_is_skipped(gitops_deploy, tick):
    gitops_deploy.write_hold(ORIGIN)
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main() == 0
    assert tick.merges == [] and tick.playbooks == []


def test_pending_ci_defers_silently(gitops_deploy, tick, state_dir):
    tick.ci = "pending"
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main() == 0
    assert tick.merges == [] and tick.posts == []
    assert _marker(state_dir, "ci_alerted_sha") is None


def test_red_ci_parks_and_pages_once_per_sha(gitops_deploy, tick, state_dir):
    tick.ci = "fail"
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main() == 0
    assert gitops_deploy.main() == 0
    assert tick.merges == []
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert _marker(state_dir, "ci_alerted_sha") == ORIGIN


# ── the diverged marker is managed every tick, ahead of the action ────────────────────────────
def test_a_diverged_checkout_is_recorded_even_on_a_dirty_tick(
    gitops_deploy, tick, state_dir
):
    tick.origin_ahead = False
    tick.local_ahead = False
    tick.dirty = True
    gitops_deploy.main()
    assert _marker(state_dir, "diverged_sha") == ORIGIN
    assert tick.merges == []


def test_an_unpushed_local_commit_is_a_plain_noop_not_a_divergence(
    gitops_deploy, tick, state_dir
):
    (state_dir / "diverged_sha").write_text(ORIGIN)
    tick.origin_ahead = False
    tick.local_ahead = True
    assert gitops_deploy.main() == 0
    assert _marker(state_dir, "diverged_sha") is None
    assert tick.merges == []


# ── the ff-merge lands the pinned SHA, on every path that merges ──────────────────────────────
def test_a_docs_only_push_ff_merges_the_pinned_sha_and_deploys_nothing(
    gitops_deploy, tick
):
    tick.paths = ["docs/runbook.md"]
    assert gitops_deploy.main() == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert tick.head == ORIGIN


# ── the Docker deploy path ────────────────────────────────────────────────────────────────────
def _docker_push(tick) -> None:
    tick.declare(DECLARES_WG_EASY)
    tick.render("wg-easy")
    tick.paths = [DOCKER_TEMPLATE]


def test_a_template_push_merges_then_deploys_then_clears_the_hold(
    gitops_deploy, tick, state_dir
):
    (state_dir / "hold_sha").write_text("f" * 40)
    _docker_push(tick)
    assert gitops_deploy.main() == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [DEPLOY_WG_EASY]
    assert tick.index("git", "merge") < tick.index("playbook", "ansible/deploy.yml"), (
        "the deploy renders from the working tree, so it must follow the merge"
    )
    assert _marker(state_dir, "hold_sha") is None
    assert tick.posts == []


def test_a_failed_health_gate_holds_then_resets_then_redeploys_the_prior_tree(
    gitops_deploy, tick, state_dir
):
    _docker_push(tick)
    tick.healthy = False
    assert gitops_deploy.main() == 0
    assert _marker(state_dir, "hold_sha") == ORIGIN
    reset = tick.index("git", "reset", "--hard", LOCAL)
    assert tick.playbooks == [DEPLOY_WG_EASY, DEPLOY_WG_EASY]
    redeploy = [i for i, e in enumerate(tick.log) if e[0] == "playbook"][1]
    assert tick.index("git", "merge") < reset < redeploy
    assert tick.head == LOCAL
    (post,) = tick.posts
    assert "rollback" in post and ORIGIN[:8] in post and LOCAL[:8] in post


def test_the_hold_is_on_disk_before_the_rollback_reset(gitops_deploy, tick, state_dir):
    # A death between the reset and the hold write would leave the next tick redeploying the
    # same bad commit, so the order is hold, then reset. Observed through the reset itself.
    _docker_push(tick)
    tick.healthy = False
    seen_at_reset: list[str | None] = []
    real_git = tick._git

    def git_with_a_look(argv):
        if argv[1] == "reset":
            seen_at_reset.append(_marker(state_dir, "hold_sha"))
        return real_git(argv)

    tick._git = git_with_a_look
    gitops_deploy.main()
    assert seen_at_reset == [ORIGIN]


@pytest.mark.parametrize(("discord_ok", "rc"), [(True, 0), (False, 1)])
def test_a_rollbacks_exit_code_follows_its_post(gitops_deploy, tick, discord_ok, rc):
    # exit 1 only when the detailed post failed, leaving OnFailure as the backstop.
    _docker_push(tick)
    tick.healthy = False
    tick.discord_ok = discord_ok
    assert gitops_deploy.main() == rc


def test_a_deploy_execution_failure_takes_the_same_rollback(
    gitops_deploy, tick, state_dir
):
    _docker_push(tick)
    tick.playbook_outcomes = [RuntimeError("ansible-playbook -> 2")]
    assert gitops_deploy.main() == 0
    assert _marker(state_dir, "hold_sha") == ORIGIN
    assert tick.playbooks == [DEPLOY_WG_EASY, DEPLOY_WG_EASY]
    assert tick.head == LOCAL
    (post,) = tick.posts
    assert "deploy failed" in post and "ansible-playbook -> 2" in post


# ── the broad planes ──────────────────────────────────────────────────────────────────────────
def test_a_setup_plane_push_merges_then_applies_its_own_playbook(
    gitops_deploy, tick, state_dir
):
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main() == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [
        [
            "uv",
            "run",
            "--frozen",
            "ansible-playbook",
            "ansible/initial_setup.yml",
            "--tags",
            "gitops_deploy",
        ]
    ]
    assert tick.log[tick.index("playbook", "ansible/initial_setup.yml")][2] == {
        "timeout": gitops_deploy.BROAD_DEPLOY_TIMEOUT_S
    }
    assert _marker(state_dir, "hold_sha") is None
    assert _marker(state_dir, "hold_plane") is None


def test_a_failed_broad_apply_holds_the_plane_and_rolls_nothing_back(
    gitops_deploy, tick, state_dir
):
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    tick.playbook_outcomes = [RuntimeError("timed out")]
    assert gitops_deploy.main() == 0
    assert _marker(state_dir, "hold_sha") == ORIGIN
    assert _marker(state_dir, "hold_plane") == "ansible/initial_setup.yml gitops_deploy"
    assert tick.head == ORIGIN, "the arm is forward-only: no reset"
    assert all(argv[1] != "reset" for argv in tick.git)
    (post,) = tick.posts
    assert "nothing was rolled back" in post


def test_a_bring_up_playbook_push_parks_and_pages(gitops_deploy, tick, state_dir):
    tick.paths = ["ansible/bootstrap.yml"]
    tick.files[f"{LOCAL}:ansible/bootstrap.yml"] = "- hosts: all\n"
    tick.files[f"{ORIGIN}:ansible/bootstrap.yml"] = "- hosts: all\n  become: true\n"
    assert gitops_deploy.main() == 0
    assert tick.merges == [] and tick.playbooks == []
    assert _marker(state_dir, "broad_alerted_sha") == ORIGIN
    assert "needing a hand" in tick.posts[0]


# ── the k8s auto-deploy path ──────────────────────────────────────────────────────────────────
def _image_bump(gitops_deploy, monkeypatch, tick, extra_paths: list[str] = ()) -> None:
    monkeypatch.setattr(gitops_deploy, "K8S_AUTODEPLOY_ENABLED", True)
    monkeypatch.setattr(gitops_deploy, "K8S_AUTODEPLOY_DENYLIST", frozenset())
    monkeypatch.setattr(gitops_deploy, "K8S_AUTODEPLOY_PILOT", frozenset())
    tick.declare(DECLARES_SONARR)
    tick.paths = [K8S_DEFAULTS, *extra_paths]
    tick.tree_listing = K8S_DEFAULTS + "\n"
    tick.files[f"{ORIGIN}:{K8S_DEFAULTS}"] = "sonarr_image: x:2\nk8s_autodeploy: true\n"
    tick.diffs["sonarr"] = "--- a\n+++ b\n-sonarr_image: x:1\n+sonarr_image: x:2\n"


DEPLOY_SONARR = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]


def test_an_image_bump_consults_staging_then_merges_then_deploys(
    gitops_deploy, monkeypatch, tick, state_dir
):
    _image_bump(gitops_deploy, monkeypatch, tick)
    assert gitops_deploy.main() == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [DEPLOY_SONARR]
    staging = tick.log.index(("staging", {"sonarr"}))
    assert staging < tick.index("git", "merge") < tick.index("playbook", "sonarr")
    assert tick.log[tick.index("playbook", "sonarr")][2] == {
        "timeout": gitops_deploy.K8S_DEPLOY_TIMEOUT_S
    }
    assert ("annotation", {"sonarr"}) in tick.log
    assert _marker(state_dir, "hold_sha") is None


def test_a_failed_rollout_rolls_back_to_the_failed_shas_snapshot_under_its_own_budget(
    gitops_deploy, monkeypatch, tick, state_dir
):
    # The snapshot worth reverting to was taken before the failed deploy and is named for the
    # commit rolled back FROM. `local` would find no snapshot on a first rollback and a stale one
    # on a second. The redeploy also reverts volumes, so it gets the larger budget.
    _image_bump(gitops_deploy, monkeypatch, tick)
    tick.playbook_outcomes = [RuntimeError("rollout gate failed")]
    assert gitops_deploy.main() == 0
    forward, rollback = (entry for entry in tick.log if entry[0] == "playbook")
    assert forward[1] == DEPLOY_SONARR
    assert rollback[1] == DEPLOY_SONARR + [
        "-e",
        f"k8s_restore_snapshot_sha={ORIGIN[:8]}",
    ]
    assert rollback[2] == {"timeout": gitops_deploy.K8S_ROLLBACK_TIMEOUT_S}
    assert _marker(state_dir, "hold_sha") == ORIGIN
    assert tick.head == LOCAL
    (post,) = tick.posts
    assert "k8s deploy failed" in post and "still live on master" in post


def test_a_secrets_change_bundled_with_an_image_bump_is_still_flagged(
    gitops_deploy, monkeypatch, tick, state_dir
):
    # The promoted service is image-bump-only by construction, so it is never the secret's
    # consumer; without the alert the rotation is ff-merged and forgotten.
    _image_bump(gitops_deploy, monkeypatch, tick, ["ansible/vars/secrets.yml"])
    assert gitops_deploy.main() == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert _marker(state_dir, "secrets_alerted_sha") == ORIGIN
    assert any("nothing was redeployed" in post for post in tick.posts)


def test_a_non_image_k8s_change_is_ff_merged_and_flagged_not_deployed(
    gitops_deploy, monkeypatch, tick, state_dir
):
    _image_bump(gitops_deploy, monkeypatch, tick)
    tick.diffs["sonarr"] = "--- a\n+++ b\n+sonarr_replicas: 2\n"
    assert gitops_deploy.main() == 0
    assert tick.merges == [ORIGIN] and tick.playbooks == []
    assert _marker(state_dir, "k8s_alerted_sha") == ORIGIN
