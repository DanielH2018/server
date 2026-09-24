#!/usr/bin/env python3
"""A broad range carrying promoted image bumps deploys them, forward-only (#2348).

`split_k8s_auto_deploy` moves an eligible bump OUT of `cs.k8s` into `cs.k8s_deploy`, and
`alert_deferred` fires its k8s channel on `cs.k8s` alone — so before #2348 a broad tick
fast-forwarded the bumps, deployed none and named none. The loss was silent, not deferred,
and the ff-merge removed the commits from every later tick's range.

Each rule is a pair: a range whose promoted half must be deployed, and a range whose k8s half
must not be — an arm that fired on every k8s path and one that fired on none read the same
from the accepting side alone.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_k8s.py
"""

import dataclasses

import deploy_locks
from _deploy_fakes import fits_budget

LOCAL = "1" * 40
ORIGIN = "2" * 40

K8S_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"
DECLARES_SONARR = "containers_list:\n  - name: sonarr\n    platform: k8s\n"

# A setup role NO playbook this deployer runs can apply: it fast-forwards and is recorded in
# `manual_plane`. The 2026-09-24 incident's range was this shape, nine bumps behind one such
# commit.
UNAPPLYABLE_ROLE = "ansible/roles/setup/k3s/tasks/main.yml"
# A setup role the deployer DOES apply, so the broad plane runs a playbook of its own and the
# ordering between the two applies is observable.
APPLYABLE_ROLE = "ansible/roles/setup/gitops_deploy/tasks/main.yml"

DEPLOY_SONARR = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]
APPLY_GITOPS_DEPLOY = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/initial_setup.yml",
    "--tags",
    "gitops_deploy",
]


def _marker(state_dir, name: str) -> str | None:
    path = state_dir / name
    return path.read_text().strip() if path.exists() else None


def _mixed(settings, tick, *broad_paths, promote: bool = True):
    """A range carrying `broad_paths` beside a sonarr image-pin bump.

    Returns the `Config` to hand `main()`. Auto-deploy is armed on that object rather than by
    `monkeypatch`ing the entry module's globals: `main(tools, config)` takes both seams, and
    the `settings` fixture has already snapshotted the scripted checkout onto the config.

    Args:
        promote: whether k8s auto-deploy is armed. False leaves the bump in `cs.k8s`, which
            is the defer-and-alert path every rejecting half below asserts against.
    """
    tick.declare(DECLARES_SONARR)
    tick.paths = [*broad_paths, K8S_DEFAULTS]
    tick.tree_listing = K8S_DEFAULTS + "\n"
    tick.files[f"{ORIGIN}:{K8S_DEFAULTS}"] = "sonarr_image: x:2\nk8s_autodeploy: true\n"
    tick.diffs["sonarr"] = "--- a\n+++ b\n-sonarr_image: x:1\n+sonarr_image: x:2\n"
    return dataclasses.replace(
        settings,
        k8s_autodeploy_enabled=promote,
        k8s_autodeploy_enabled_in_file=promote,
        k8s_autodeploy_denylist=frozenset(),
        k8s_autodeploy_pilot=frozenset(),
    )


# ── the promoted half is deployed, after the plane under it ───────────────────────────────


def test_a_mixed_range_applies_the_setup_plane_then_deploys_the_bump(
    gitops_deploy, tick, settings, state_dir
):
    """Both applies run, in that order, and the bump gets the k8s budget rather than the broad one.

    The order is the one `main()`'s broad-before-k8s branch exists to hold: a workload applied
    onto a host whose setup plane has not applied is the state the ordering prevents.
    """
    config = _mixed(settings, tick, APPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY, DEPLOY_SONARR]
    assert tick.index("git", "merge") < tick.index("playbook", "sonarr")
    deployed = tick.log[tick.index("playbook", "sonarr")][2]
    assert fits_budget(deployed, gitops_deploy.K8S_DEPLOY_TIMEOUT_S)
    assert ("annotation", {"sonarr"}) in tick.log
    assert _marker(state_dir, "hold_sha") is None


def test_a_setup_role_the_deployer_cannot_apply_still_deploys_the_bump(
    gitops_deploy, tick, settings, state_dir
):
    """#2348 itself: the range that lost nine bumps behind one `roles/setup/k3s` commit.

    The role is recorded in `manual_plane` and owed to a hand — and that says nothing about
    the image bumps beside it, which nothing in `roles/setup/k3s` gates.
    """
    config = _mixed(settings, tick, UNAPPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert tick.merges == [ORIGIN]
    assert "k3s" in _marker(state_dir, "manual_plane")
    assert ("annotation", {"sonarr"}) in tick.log


# ── and a range with nothing promoted deploys nothing ─────────────────────────────────────


def test_an_unapplyable_setup_role_alone_runs_no_playbook(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half: with no bump in the range there is nothing for the new arm to run."""
    config = _mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.paths = [UNAPPLYABLE_ROLE]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == []
    assert "k3s" in _marker(state_dir, "manual_plane")


def test_a_bump_auto_deploy_never_promoted_is_deferred_not_deployed(
    gitops_deploy, tick, settings, state_dir
):
    """The second rejecting half: the arm keys on `cs.k8s_deploy`, not on any k8s path.

    With auto-deploy disarmed the same range leaves sonarr in `cs.k8s`, which is the
    defer-and-alert channel it has always taken.
    """
    config = _mixed(settings, tick, UNAPPLYABLE_ROLE, promote=False)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == []
    assert _marker(state_dir, "k8s_alerted_sha") == ORIGIN


# ── a failed bump holds the SHA and rolls nothing back ────────────────────────────────────


def test_a_failed_bump_holds_the_sha_names_no_plane_and_does_not_reset(
    gitops_deploy, tick, settings, state_dir
):
    """Forward-only. A reset here would undo the ff-merge under an applied setup plane.

    No `hold_plane` either: that marker names a playbook for `clear_broad_hold` to match an
    apply against, and a service is not a plane — a hold written there is one no broad apply
    can clear.
    """
    config = _mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("image manifest unknown")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == ORIGIN, "the tree stays fast-forwarded"
    assert _marker(state_dir, "hold_sha") == ORIGIN
    assert _marker(state_dir, "hold_plane") is None
    assert "Nothing was rolled back" in tick.posts[-1]


def test_a_failed_broad_apply_never_reaches_the_bump(
    gitops_deploy, tick, settings, state_dir
):
    """The plane below has to succeed first, so its failure arm returns before the k8s deploy."""
    config = _mixed(settings, tick, APPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY]
    assert _marker(state_dir, "hold_plane") == "ansible/initial_setup.yml gitops_deploy"


def test_a_busy_service_lock_undoes_the_range_and_the_manual_plane_line(
    gitops_deploy, tick, settings, state_dir
):
    """Contention is not a failed deploy: nothing ran, so the whole range goes back.

    The `manual_plane` line this tick wrote goes back with it — the reset undoes the ff-merge,
    so the line would describe a range no tree carries.
    """
    config = _mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy("service lock sonarr busy")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert _marker(state_dir, "hold_sha") is None
    assert _marker(state_dir, "manual_plane") is None
    assert _marker(state_dir, "contention_since").startswith(ORIGIN)
