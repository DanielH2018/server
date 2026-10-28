"""main()'s branches, run against a scripted checkout.

Each invariant is an assertion on what main() did: the ff-merge lands the SHA
the tick pinned, the diverged marker is written ahead of the action branching,
drain_pending() runs ahead of the short-circuits, a broad apply holds its plane and rolls
nothing back, every exit that leaves a range merged sends the tasks and secrets pages, and a
deploy-plane path applies nothing on this host. The `tick` fixture in conftest.py answers git, ansible-playbook, the
CI verdict, the health gate and Discord from a script and records every
call in order.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_branches.py

import json

import pytest

from deployer_lib import deploy_alerts, deploy_locks
from _deploy_fakes import fits_budget
from gitops_ledger import OWED_HOLD_PLANE, owed_line
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
    """The SHA one alert slot has paged on, out of the one keyed marker file."""
    return parse_alerted(_marker(state_dir, "alerted_shas")).get(slot)


# ── the short-circuits: nothing merges, nothing deploys ───────────────────────────────────────
def test_a_converged_checkout_is_a_noop(gitops_deploy, tick, state):
    tick.origin = tick.local
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [] and tick.playbooks == [] and tick.posts == []


def test_drain_pending_runs_ahead_of_the_noop_short_circuit(
    gitops_deploy, tick, state_dir, state
):
    # The ff-merged channels never re-reach their alert code on a later tick, so a queued alert
    # is only recoverable at the top of EVERY tick, before local == origin returns.
    deploy_alerts.write_pending(
        state.path("pending_alerts"),
        {"secrets:" + ORIGIN: "queued last tick"},
    )
    tick.origin = tick.local
    gitops_deploy.main(tick.tools, tick.config, state)
    assert tick.posts == ["queued last tick"]
    assert json.loads((state_dir / "pending_alerts.json").read_text()) == {}


def test_the_detach_spool_is_flushed_on_a_noop_and_a_dirty_tick(
    gitops_deploy, tick, state_dir, state
):
    # `deploy --detach`'s notifier runs once and queues an undelivered verdict for this tick to
    # send (#3987). A noop tick and a dirty one are the two this host spends most ticks in.
    spool = str(state_dir / deploy_alerts.DETACH_SPOOL)
    tick.origin = tick.local
    gitops_deploy.main(tick.tools, tick.config, state)
    tick.dirty = True
    gitops_deploy.main(tick.tools, tick.config, state)
    assert [e[1] for e in tick.log if e[0] == "flush"] == [spool, spool]


def test_a_dirty_tree_skips_without_merging(gitops_deploy, tick, state):
    tick.dirty = True
    tick.paths = ["docs/x.md"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [] and tick.playbooks == []


def test_a_held_sha_is_skipped(gitops_deploy, tick, state):
    state.write_hold(ORIGIN)
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [] and tick.playbooks == []


def test_pending_ci_defers_silently(gitops_deploy, tick, state_dir, state):
    tick.ci = "pending"
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [] and tick.posts == []
    assert _alerted(state_dir, "ci") is None


def test_red_ci_parks_and_pages_once_per_sha(gitops_deploy, tick, state_dir, state):
    tick.ci = "fail"
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == []
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert _alerted(state_dir, "ci") == ORIGIN


def test_a_red_tip_over_a_green_ancestor_deploys_the_ancestor_and_still_pages(
    gitops_deploy, tick, state_dir, state
):
    """The tick deploys what it can and the red tip is still reported, once for that SHA.

    Without the page a red master would go unreported the moment any earlier commit was
    green, which is the signal the CI gate exists to raise.
    """
    tick.ci = "fail"
    tick.rev_list = [ORIGIN, GREEN_ANCESTOR]
    tick.ancestor_ci = {GREEN_ANCESTOR: "pass"}
    tick.paths = ["docs/runbook.md"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    # The second tick converges on the same ancestor, so it is a noop and pages nothing new.
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [GREEN_ANCESTOR]
    assert len(tick.posts) == 1 and "CI is RED" in tick.posts[0]
    assert _alerted(state_dir, "ci") == ORIGIN


# ── the diverged marker is managed every tick, ahead of the action ────────────────────────────
def test_a_diverged_checkout_is_recorded_even_on_a_dirty_tick(
    gitops_deploy, tick, state_dir, state
):
    tick.origin_ahead = False
    tick.local_ahead = False
    tick.dirty = True
    gitops_deploy.main(tick.tools, tick.config, state)
    assert _marker(state_dir, "diverged_sha") == ORIGIN
    assert tick.merges == []


def test_an_unpushed_local_commit_is_a_plain_noop_not_a_divergence(
    gitops_deploy, tick, state_dir, state
):
    (state_dir / "diverged_sha").write_text(ORIGIN)
    tick.origin_ahead = False
    tick.local_ahead = True
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _marker(state_dir, "diverged_sha") is None
    assert tick.merges == []


# ── the ff-merge lands the pinned SHA, on every path that merges ──────────────────────────────
def test_a_docs_only_push_ff_merges_the_pinned_sha_and_deploys_nothing(
    gitops_deploy, tick, state
):
    tick.paths = ["docs/runbook.md"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert tick.head == ORIGIN


# ── a Docker role change ──────────────────────────────────────────────────────────────────────
def test_a_docker_template_push_merges_deploys_nothing_and_says_so(
    gitops_deploy, tick, state_dir, capsys, state
):
    """No has_gitops host runs Docker, so a Pi role change is merged and left to a hand deploy.

    The hold stays: nothing was deployed, so nothing proved the held SHA fixed.
    """
    (state_dir / "hold_sha").write_text("f" * 40)
    tick.declare(DECLARES_WG_EASY)
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert _marker(state_dir, "hold_sha") == "f" * 40
    assert "does not deploy: ['wg-easy']" in capsys.readouterr().out


def test_a_containers_common_push_merges_without_the_full_play(
    gitops_deploy, tick, capsys, state
):
    """The Pi's shared deploy path is not broad, so it does not buy a full deploy.yml."""
    tick.paths = ["ansible/roles/containers/common/tasks/docker_deploy.yml"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert tick.playbooks == [] and tick.posts == []
    assert "-e target=daniel-pi" in capsys.readouterr().out


# ── the broad planes ──────────────────────────────────────────────────────────────────────────
def test_a_setup_plane_push_merges_then_applies_its_own_playbook(
    gitops_deploy, tick, state_dir, state
):
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
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
    assert state.hold_plane is None


def test_a_failed_broad_apply_holds_the_plane_and_rolls_nothing_back(
    gitops_deploy, tick, state_dir, state
):
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    tick.playbook_outcomes = [RuntimeError("timed out")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _marker(state_dir, "hold_sha") == ORIGIN
    assert state.hold_plane == "ansible/initial_setup.yml gitops_deploy"
    assert tick.head == ORIGIN, "the arm is forward-only: no reset"
    assert all(argv[1] != "reset" for argv in tick.git)
    (post,) = tick.posts
    assert "nothing was rolled back" in post


def test_a_bring_up_playbook_push_parks_and_pages(
    gitops_deploy, tick, state_dir, state
):
    tick.paths = ["ansible/bootstrap.yml"]
    tick.files[f"{LOCAL}:ansible/bootstrap.yml"] = "- hosts: all\n"
    tick.files[f"{ORIGIN}:ansible/bootstrap.yml"] = "- hosts: all\n  become: true\n"
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [] and tick.playbooks == []
    assert _alerted(state_dir, "broad") == ORIGIN
    assert "needing a hand" in tick.posts[0]


# ── a hold clears only when its own plane is applied ──────────────────────────────────────────
def _hold_the_deploy_plane(state_dir) -> None:
    """The state a failed `ansible/deploy.yml` broad apply leaves behind."""
    (state_dir / "hold_sha").write_text("f" * 40)
    (state_dir / "owed.jsonl").write_text(
        owed_line(OWED_HOLD_PLANE, "ansible/deploy.yml", "f" * 40, 1)
    )


def test_a_setup_plane_success_keeps_a_deploy_plane_hold(
    gitops_deploy, tick, state_dir, state
):
    """A held deploy.yml survives a successful setup-plane tick.

    Clearing both markers would leave the deploy plane unapplied, and every consumer gates on
    hold_sha — so GitOps Deploy — Status would read green over it.
    """
    _hold_the_deploy_plane(state_dir)
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks, "the setup plane still applies"
    assert _marker(state_dir, "hold_sha") == "f" * 40
    assert state.hold_plane == "ansible/deploy.yml"


def test_applying_the_held_plane_clears_the_hold(gitops_deploy, tick, state_dir, state):
    """The converse, so the guard is not simply "never clears"."""
    (state_dir / "hold_sha").write_text("f" * 40)
    (state_dir / "owed.jsonl").write_text(
        owed_line(
            OWED_HOLD_PLANE, "ansible/initial_setup.yml gitops_deploy", "f" * 40, 1
        )
    )
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _marker(state_dir, "hold_sha") is None
    assert state.hold_plane is None


# ── the host-plane pages, now that no k8s module carries them (D6.12) ─────────────────────────
WG_EASY_TASKS = "ansible/roles/containers/wg-easy/tasks/main.yml"
SETUP_TEMPLATE = "ansible/roles/setup/gitops_deploy/templates/config.env.j2"
SECRETS = "ansible/vars/secrets.yml"


def test_a_pi_role_tasks_change_pages_the_tasks_channel_once_per_origin(
    gitops_deploy, tick, state_dir, state
):
    """A `tasks/` change is merged and named, never merged in silence.

    The second tick re-crosses the same range at the same origin and posts nothing, because
    the slot already holds that SHA.
    """
    tick.declare(DECLARES_WG_EASY)
    tick.paths = [WG_EASY_TASKS]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert _alerted(state_dir, "tasks") == ORIGIN
    (post,) = tick.posts
    assert "wg-easy" in post and "not applied" in post
    tick.head = LOCAL
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert len(tick.posts) == 1


def test_a_pi_role_template_change_pages_no_tasks_channel(
    gitops_deploy, tick, state_dir, state
):
    tick.declare(DECLARES_WG_EASY)
    tick.paths = [DOCKER_TEMPLATE]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert _alerted(state_dir, "tasks") is None
    assert tick.posts == []


def _secrets_posts(tick) -> list[str]:
    return [post for post in tick.posts if "`secrets.yml` changed" in post]


# `applied` reaches the tail of `handle_broad`, `apply-failed` its failure arm. Both leave the
# range merged, so both owe the page.
BROAD_EXITS = pytest.mark.parametrize(
    "outcomes", [[], [RuntimeError("timed out")]], ids=["applied", "apply-failed"]
)


@BROAD_EXITS
def test_a_broad_setup_range_with_a_secret_pages_the_secrets_channel(
    gitops_deploy, tick, state_dir, state, outcomes
):
    tick.paths = [SETUP_TEMPLATE, SECRETS]
    tick.playbook_outcomes = list(outcomes)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert _alerted(state_dir, "secrets") == ORIGIN
    assert len(_secrets_posts(tick)) == 1


@BROAD_EXITS
def test_a_broad_setup_range_without_a_secret_pages_no_secrets_channel(
    gitops_deploy, tick, state_dir, state, outcomes
):
    tick.paths = [SETUP_TEMPLATE]
    tick.playbook_outcomes = list(outcomes)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _alerted(state_dir, "secrets") is None
    assert _secrets_posts(tick) == []


def test_a_contended_tick_pages_no_secret_until_the_retry_merges(
    gitops_deploy, tick, state_dir, state
):
    """The contention arm resets the ff-merge, so its page would be false (#2459).

    The post says the range was fast-forwarded. After a reset the tree is back on `local`,
    so the page belongs to the tick that leaves the range merged, which is the retry.
    """
    tick.paths = [SETUP_TEMPLATE, SECRETS]
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy("service lock busy")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert _secrets_posts(tick) == []
    assert _alerted(state_dir, "secrets") is None
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.head == ORIGIN, "the retry merged and applied"
    assert len(_secrets_posts(tick)) == 1, "the retry owes exactly one page"
    assert _alerted(state_dir, "secrets") == ORIGIN


# ── a deploy-plane path applies nothing on this host (D6.12) ─────────────────────────────────
def test_an_inventory_change_runs_no_deploy_playbook_and_owes_nothing(
    gitops_deploy, tick, state_dir, state
):
    """The renderer reads `ansible/inventory/`, so the cluster half reaches the cluster through
    `promote`. The tick fast-forwards past the path as it passes a docs-only one: no
    `deploy.yml`, no receipt naming it, no ledger line.
    """
    tick.paths = ["ansible/inventory/group_vars/all.yml"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert [argv for argv in tick.playbooks if "ansible/deploy.yml" in argv] == []
    assert "ansible/deploy.yml" not in (state.read("receipts") or "")
    assert not (state.read("owed") or "").strip()
    assert _marker(state_dir, "hold_sha") is None
