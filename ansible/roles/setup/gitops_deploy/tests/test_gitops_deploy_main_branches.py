"""main()'s branches, run against a scripted checkout.

Every invariant here used to be an AST guard on main()'s source: the ff-merge lands the SHA
the tick pinned, write_hold precedes every rollback reset, a rollback's exit code follows
whether its post was delivered, the diverged marker is written ahead of the action
branching, drain_pending() runs ahead of the short-circuits, the rollback redeploy passes the
FAILED commit's short SHA under its own budget, and a secrets change bundled with an image
bump is still flagged. The `tick` fixture in conftest.py answers git, ansible-playbook, the
CI verdict, the health gate and Discord from a script and records every
call in order, so each guard is now an assertion on what main() did.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_branches.py

import json
from collections.abc import Sequence


import deploy_alerts
from _deploy_fakes import fits_budget
from gitops_markers import parse_alerted

# The SHAs the `tick` fixture starts from; `from conftest import` is avoided because the
# repo has several conftest.py files and the name resolves to whichever sys.path saw first.
LOCAL = "1" * 40
ORIGIN = "2" * 40
# One commit between LOCAL and the tip, for the ticks that fast-forward past a tip that is
# pending or red.
GREEN_ANCESTOR = "3" * 40
DOCKER_TEMPLATE = "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"
K8S_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"
DECLARES_WG_EASY = "containers_list:\n  - name: wg-easy\n    platform: docker\n"
DECLARES_SONARR = "containers_list:\n  - name: sonarr\n    platform: k8s\n"


def _marker(state_dir, name: str) -> str | None:
    path = state_dir / name
    return path.read_text().strip() if path.exists() else None


def _alerted(state_dir, slot: str) -> str | None:
    """The SHA one alert slot has paged on, out of the one keyed marker file (#3047)."""
    return parse_alerted(_marker(state_dir, "alerted_shas")).get(slot)


# ── the short-circuits: nothing merges, nothing deploys ───────────────────────────────────────
def test_a_converged_checkout_is_a_noop(gitops_deploy, tick):
    tick.origin = tick.local
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [] and tick.playbooks == [] and tick.posts == []


def test_drain_pending_runs_ahead_of_the_noop_short_circuit(
    gitops_deploy, tick, state_dir
):
    # The ff-merged channels never re-reach their alert code on a later tick, so a queued alert
    # is only recoverable at the top of EVERY tick, before local == origin returns.
    deploy_alerts.write_pending(
        gitops_deploy.STATE.path("pending_alerts"),
        {"secrets:" + ORIGIN: "queued last tick"},
    )
    tick.origin = tick.local
    gitops_deploy.main(tick.tools)
    assert tick.posts == ["queued last tick"]
    assert json.loads((state_dir / "pending_alerts.json").read_text()) == {}


def test_a_dirty_tree_skips_without_merging(gitops_deploy, tick):
    tick.dirty = True
    tick.paths = ["docs/x.md"]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [] and tick.playbooks == []


def test_a_held_sha_is_skipped(gitops_deploy, tick):
    gitops_deploy.STATE.write_hold(ORIGIN)
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [] and tick.playbooks == []


def test_pending_ci_defers_silently(gitops_deploy, tick, state_dir):
    tick.ci = "pending"
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [] and tick.posts == []
    assert _alerted(state_dir, "ci") is None


def test_red_ci_parks_and_pages_once_per_sha(gitops_deploy, tick, state_dir):
    tick.ci = "fail"
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools) == 0
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == []
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert _alerted(state_dir, "ci") == ORIGIN


def test_a_red_tip_over_a_green_ancestor_deploys_the_ancestor_and_still_pages(
    gitops_deploy, tick, state_dir
):
    """The tick deploys what it can and the red tip is still reported, once for that SHA.

    Without the page a red master would go unreported the moment any earlier commit was
    green, which is the signal the CI gate exists to raise.
    """
    tick.ci = "fail"
    tick.rev_list = [ORIGIN, GREEN_ANCESTOR]
    tick.ancestor_ci = {GREEN_ANCESTOR: "pass"}
    tick.paths = ["docs/runbook.md"]
    assert gitops_deploy.main(tick.tools) == 0
    # The second tick converges on the same ancestor, so it is a noop and pages nothing new.
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [GREEN_ANCESTOR]
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert _alerted(state_dir, "ci") == ORIGIN


# ── the diverged marker is managed every tick, ahead of the action ────────────────────────────
def test_a_diverged_checkout_is_recorded_even_on_a_dirty_tick(
    gitops_deploy, tick, state_dir
):
    tick.origin_ahead = False
    tick.local_ahead = False
    tick.dirty = True
    gitops_deploy.main(tick.tools)
    assert _marker(state_dir, "diverged_sha") == ORIGIN
    assert tick.merges == []


def test_an_unpushed_local_commit_is_a_plain_noop_not_a_divergence(
    gitops_deploy, tick, state_dir
):
    (state_dir / "diverged_sha").write_text(ORIGIN)
    tick.origin_ahead = False
    tick.local_ahead = True
    assert gitops_deploy.main(tick.tools) == 0
    assert _marker(state_dir, "diverged_sha") is None
    assert tick.merges == []


# ── the ff-merge lands the pinned SHA, on every path that merges ──────────────────────────────
def test_a_docs_only_push_ff_merges_the_pinned_sha_and_deploys_nothing(
    gitops_deploy, tick
):
    tick.paths = ["docs/runbook.md"]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert tick.head == ORIGIN


# ── a Docker role change ──────────────────────────────────────────────────────────────────────
def test_a_docker_template_push_merges_deploys_nothing_and_says_so(
    gitops_deploy, tick, state_dir, capsys
):
    """No has_gitops host runs Docker, so a Pi role change is merged and left to a hand deploy.

    The hold stays: nothing was deployed, so nothing proved the held SHA fixed.
    """
    (state_dir / "hold_sha").write_text("f" * 40)
    tick.declare(DECLARES_WG_EASY)
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert _marker(state_dir, "hold_sha") == "f" * 40
    assert "does not deploy: ['wg-easy']" in capsys.readouterr().out


def test_a_containers_common_push_merges_without_the_full_play(
    gitops_deploy, tick, capsys
):
    """The Pi's shared deploy path used to read as broad and buy a full deploy.yml here (#2805)."""
    tick.paths = ["ansible/roles/containers/common/tasks/docker_deploy.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert "-e target=daniel-pi" in capsys.readouterr().out


# ── the broad planes ──────────────────────────────────────────────────────────────────────────
def test_a_setup_plane_push_merges_then_applies_its_own_playbook(
    gitops_deploy, tick, state_dir
):
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools) == 0
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
    applied = tick.log[tick.index("playbook", "ansible/initial_setup.yml")][2]
    assert fits_budget(applied, gitops_deploy.BROAD_DEPLOY_TIMEOUT_S)
    assert _marker(state_dir, "hold_sha") is None
    assert _marker(state_dir, "hold_plane") is None


def test_a_failed_broad_apply_holds_the_plane_and_rolls_nothing_back(
    gitops_deploy, tick, state_dir
):
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    tick.playbook_outcomes = [RuntimeError("timed out")]
    assert gitops_deploy.main(tick.tools) == 0
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
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [] and tick.playbooks == []
    assert _alerted(state_dir, "broad") == ORIGIN
    assert "needing a hand" in tick.posts[0]


# ── a hold clears only when its own plane is applied ──────────────────────────────────────────
def _hold_the_deploy_plane(state_dir) -> None:
    """The state a failed `ansible/deploy.yml` broad apply leaves behind."""
    (state_dir / "hold_sha").write_text("f" * 40)
    (state_dir / "hold_plane").write_text("ansible/deploy.yml")


def test_a_setup_plane_success_keeps_a_deploy_plane_hold(
    gitops_deploy, tick, state_dir
):
    """The 2026-09-02 erasure: a held deploy.yml, then a successful setup-plane tick.

    Both markers went within 30 seconds while the deploy plane stayed unapplied, and every
    consumer gates on hold_sha — so GitOps Deploy — Status read green over it.
    """
    _hold_the_deploy_plane(state_dir)
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.playbooks, "the setup plane still applies"
    assert _marker(state_dir, "hold_sha") == "f" * 40
    assert _marker(state_dir, "hold_plane") == "ansible/deploy.yml"


def test_applying_the_held_plane_clears_the_hold(gitops_deploy, tick, state_dir):
    """The converse, so the guard is not simply "never clears"."""
    (state_dir / "hold_sha").write_text("f" * 40)
    (state_dir / "hold_plane").write_text("ansible/initial_setup.yml gitops_deploy")
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools) == 0
    assert _marker(state_dir, "hold_sha") is None
    assert _marker(state_dir, "hold_plane") is None


# ── the k8s auto-deploy path ──────────────────────────────────────────────────────────────────
def _image_bump(
    gitops_deploy, monkeypatch, tick, extra_paths: Sequence[str] = ()
) -> None:
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


def test_an_image_bump_beside_a_pi_change_deploys_and_names_the_pi_half(
    gitops_deploy, monkeypatch, tick, capsys
):
    """#2836: the bump deploys, and the Pi work it rode in with is still named.

    The promotion used to be refused outright when `cs.services` was non-empty. Dropping that
    moves the tick off `handle_no_services`, which was the only path that said anything about
    the Pi half — so `handle_k8s` says it now.
    """
    _image_bump(gitops_deploy, monkeypatch, tick, extra_paths=[DOCKER_TEMPLATE])
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.playbooks == [DEPLOY_SONARR], "the bump deployed rather than deferring"
    assert "does not deploy: ['wg-easy']" in capsys.readouterr().out


def test_an_image_bump_merges_then_deploys(gitops_deploy, monkeypatch, tick, state_dir):
    _image_bump(gitops_deploy, monkeypatch, tick)
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [DEPLOY_SONARR]
    assert tick.index("git", "merge") < tick.index("playbook", "sonarr")
    deployed = tick.log[tick.index("playbook", "sonarr")][2]
    assert fits_budget(deployed, gitops_deploy.K8S_DEPLOY_TIMEOUT_S)
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
    assert gitops_deploy.main(tick.tools) == 0
    forward, rollback = (entry for entry in tick.log if entry[0] == "playbook")
    assert forward[1] == DEPLOY_SONARR
    assert rollback[1] == DEPLOY_SONARR + [
        "-e",
        f"k8s_restore_snapshot_sha={ORIGIN[:8]}",
    ]
    assert fits_budget(rollback[2], gitops_deploy.K8S_ROLLBACK_TIMEOUT_S)
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
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert _alerted(state_dir, "secrets") == ORIGIN
    assert any("nothing was redeployed" in post for post in tick.posts)


def test_a_non_image_k8s_change_is_ff_merged_and_flagged_not_deployed(
    gitops_deploy, monkeypatch, tick, state_dir
):
    _image_bump(gitops_deploy, monkeypatch, tick)
    tick.diffs["sonarr"] = "--- a\n+++ b\n+sonarr_replicas: 2\n"
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.merges == [ORIGIN] and tick.playbooks == []
    assert _alerted(state_dir, "k8s") == ORIGIN
