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

import pytest

import deploy_locks
from _broad_k8s_range import (
    alerted,
    APPLY_GITOPS_DEPLOY,
    APPLYABLE_ROLE,
    DEPLOY_PLANE,
    DEPLOY_PLANE_FULL,
    DEPLOY_SONARR,
    LOCAL,
    ORIGIN,
    UNAPPLYABLE_ROLE,
    marker,
    mixed,
    plane_applies_radarr,
)
from _deploy_fakes import fits_budget

# ── the promoted half is deployed, after the plane under it ───────────────────────────────


def test_a_mixed_range_applies_the_setup_plane_then_deploys_the_bump(
    gitops_deploy, tick, settings, state_dir
):
    """Both applies run, in that order, and both come out of the one broad budget.

    The order is the one `main()`'s broad-before-k8s branch exists to hold: a workload applied
    onto a host whose setup plane has not applied is the state the ordering prevents. The
    shared budget is what keeps the unit's ceiling reading this arm as a single apply, so the
    bump must NOT get a `K8S_DEPLOY_TIMEOUT_S` of its own on top of it.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY, DEPLOY_SONARR]
    assert tick.index("git", "merge") < tick.index("playbook", "sonarr")
    deployed = tick.log[tick.index("playbook", "sonarr")][2]
    assert fits_budget(deployed, gitops_deploy.BROAD_DEPLOY_TIMEOUT_S)
    assert ("annotation", {"sonarr"}) in tick.log
    assert marker(state_dir, "hold_sha") is None


def test_a_setup_role_the_deployer_cannot_apply_still_deploys_the_bump(
    gitops_deploy, tick, settings, state_dir
):
    """#2348 itself: the range that lost nine bumps behind one `roles/setup/k3s` commit.

    The role is recorded in `manual_plane` and owed to a hand — and that says nothing about
    the image bumps beside it, which nothing in `roles/setup/k3s` gates.
    """
    config = mixed(settings, tick, UNAPPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert tick.merges == [ORIGIN]
    assert "k3s" in marker(state_dir, "manual_plane")
    assert ("annotation", {"sonarr"}) in tick.log


# ── and a range with nothing promoted deploys nothing ─────────────────────────────────────


def test_an_unapplyable_setup_role_alone_runs_no_playbook(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half: with no bump in the range there is nothing for the new arm to run."""
    config = mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.paths = [UNAPPLYABLE_ROLE]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == []
    assert "k3s" in marker(state_dir, "manual_plane")


def test_a_bump_auto_deploy_never_promoted_is_deferred_not_deployed(
    gitops_deploy, tick, settings, state_dir
):
    """The second rejecting half: the arm keys on `cs.k8s_deploy`, not on any k8s path.

    With auto-deploy disarmed the same range leaves sonarr in `cs.k8s`, which is the
    defer-and-alert channel it has always taken.
    """
    config = mixed(settings, tick, UNAPPLYABLE_ROLE, promote=False)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == []
    assert alerted(state_dir, "k8s") == ORIGIN


# ── a failed bump holds the SHA and rolls nothing back ────────────────────────────────────


def test_a_failed_bump_holds_the_sha_and_its_plane_and_does_not_reset(
    gitops_deploy, tick, settings, state_dir
):
    """Forward-only. A reset here would undo the ff-merge under an applied setup plane.

    The hold names the run that failed, `deploy.yml --tags sonarr`. With no `hold_plane` the
    next unrelated service deploy cleared it through `clear_service_hold`, and GitOps Deploy —
    Status went green over the failed pin.
    """
    config = mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("image manifest unknown")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == ORIGIN, "the tree stays fast-forwarded"
    assert marker(state_dir, "hold_sha") == ORIGIN
    assert marker(state_dir, "hold_plane") == "ansible/deploy.yml sonarr"
    assert "Nothing was rolled back" in tick.posts[-1]


# ── a bump is deployed once, and only with a budget that fits it ──────────────────────────


@pytest.mark.parametrize(
    ("narrow", "expected"),
    [((0, "sonarr"), [DEPLOY_SONARR]), ((3, ""), [DEPLOY_PLANE_FULL])],
    ids=["narrowed-to-the-bump", "refused-full-run"],
)
def test_a_bump_the_deploy_plane_applies_is_not_deployed_again(
    gitops_deploy, tick, settings, narrow, expected
):
    """A second `--tags sonarr` re-takes the Longhorn snapshot and spends the shared budget."""
    config = mixed(settings, tick, DEPLOY_PLANE)
    tick.narrow = narrow
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == expected
    assert ("annotation", {"sonarr"}) in tick.log


def test_a_deploy_plane_narrowed_elsewhere_still_deploys_the_bump(
    gitops_deploy, tick, settings
):
    """The rejecting half: a plane that does not name the bump's tag does not cover it."""
    config = mixed(settings, tick, DEPLOY_PLANE)
    tick.narrow = (0, "radarr")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [[*DEPLOY_SONARR[:-1], "radarr"], DEPLOY_SONARR]


def test_a_bump_the_remaining_budget_cannot_fit_is_deferred(
    gitops_deploy, tick, settings, state_dir
):
    """Less left than a k8s-only tick grants a bump: defer it, never start a run to be killed.

    No hold and no reset, because the plane under it applied. The bump takes the
    defer-and-alert channel, whose durable half is `Release Staleness Drift` reading the pin.
    """
    config = dataclasses.replace(
        mixed(settings, tick, APPLYABLE_ROLE),
        broad_deploy_timeout_s=60,
        k8s_deploy_timeout_s=900,
    )
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY]
    assert tick.head == ORIGIN
    assert marker(state_dir, "hold_sha") is None
    assert alerted(state_dir, "k8s") == ORIGIN
    deferral = next(post for post in tick.posts if "sonarr" in post)
    assert "`k8s_autodeploy`" in deferral, (
        "the deferral post names the auto-deploy this bump was eligible for, rather than "
        "claiming the deployer never auto-deploys a k8s bump"
    )


# ── a new failure never overwrites a hold another plane still owes ────────────────────────


@pytest.mark.parametrize(
    ("earlier", "broad_path", "outcomes"),
    [
        ("ansible/deploy.yml", UNAPPLYABLE_ROLE, [RuntimeError("bump failed")]),
        (
            "ansible/initial_setup.yml gitops_deploy",
            UNAPPLYABLE_ROLE,
            [RuntimeError("bump failed")],
        ),
        (
            "ansible/deploy.yml radarr",
            APPLYABLE_ROLE,
            [None, RuntimeError("bump failed")],
        ),
    ],
    ids=["untagged-deploy-plane", "setup-plane", "narrowed-deploy-plane"],
)
def test_a_failed_bump_keeps_an_earlier_plane_held_past_the_bumps_fix(
    gitops_deploy, tick, settings, state_dir, earlier, broad_path, outcomes
):
    """#878's class: the bump's fix-forward deploy must clear the bump's entry and no other.

    Overwriting `hold_plane` with the bump's entry let a later sonarr deploy clear both
    markers while the earlier plane was still unapplied, and GitOps Deploy — Status read green.
    """
    (state_dir / "hold_sha").write_text("e" * 40)
    (state_dir / "hold_plane").write_text(earlier)
    config = mixed(settings, tick, broad_path)
    tick.playbook_outcomes = outcomes
    assert gitops_deploy.main(tick.tools, config) == 0
    gitops_deploy.STATE.clear_service_hold({"sonarr"})
    assert marker(state_dir, "hold_sha") is not None, "the earlier plane is still owed"
    assert marker(state_dir, "hold_plane") == earlier


def test_a_failed_plane_keeps_an_earlier_plane_held_beside_its_own(
    gitops_deploy, tick, settings, state_dir
):
    """The broad loop's own failure arm overwrote the marker the same way."""
    (state_dir / "hold_sha").write_text("e" * 40)
    (state_dir / "hold_plane").write_text("ansible/deploy.yml radarr")
    config = mixed(settings, tick, APPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config) == 0
    gitops_deploy.STATE.clear_broad_hold("ansible/initial_setup.yml", ["gitops_deploy"])
    assert marker(state_dir, "hold_sha") == ORIGIN
    assert marker(state_dir, "hold_plane") == "ansible/deploy.yml radarr"


def test_a_failed_bump_still_annotates_the_bump_the_plane_applied(
    gitops_deploy, tick, settings
):
    """radarr went out with the narrowed deploy plane; sonarr's failure must not hide that."""
    config = plane_applies_radarr(settings, tick)
    tick.playbook_outcomes = [None, RuntimeError("image manifest unknown")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks[-1] == DEPLOY_SONARR, "sonarr was the bump that failed"
    assert ("annotation", {"radarr"}) in tick.log


def test_a_contended_bump_annotates_nothing_the_next_tick_will_annotate_again(
    gitops_deploy, tick, settings
):
    """#2453: the reset makes the plane's own apply something the next tick redoes.

    radarr really did go out under this tick, and the annotation is still wrong: the tree is
    back on `local`, the next tick re-crosses the range and re-applies the same plane, and
    Grafana drew two deploy annotations for one deploy.
    """
    config = plane_applies_radarr(settings, tick)
    tick.playbook_outcomes = [None, deploy_locks.ServiceLockBusy("lock sonarr busy")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert not [entry for entry in tick.log if entry[0] == "annotation"]


# What the plan loop records for `plane_applies_radarr`'s narrowed deploy plane, and what the
# bump's contention arm below has to take back.
PLANE_APPLIED = f"{ORIGIN} ansible/deploy.yml radarr"


def test_a_contended_bump_takes_back_the_planes_broad_applied(
    gitops_deploy, tick, settings, state_dir
):
    """#2382's widened path (#2459): the bump's own contention arm restores the marker too.

    The loop's arm was pinned when #2382 landed; this one never was. The plane applied and
    recorded `broad_applied`, then the bump's lock wait reset the tree to `local` — and
    `land.sh` reads that marker to tell a plane the tick APPLIED from one it merely
    fast-forwarded past (#1537), which after the reset this tree carries neither of.
    """
    config = plane_applies_radarr(settings, tick)
    tick.playbook_outcomes = [None, deploy_locks.ServiceLockBusy("lock sonarr busy")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert marker(state_dir, "broad_applied") != PLANE_APPLIED
    assert marker(state_dir, "broad_applied") is None


def test_a_contended_bump_leaves_an_earlier_ticks_broad_applied_alone(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half: the reverse RESTORES the earlier value, it does not clear.

    An earlier tick's record is true — that plane was applied and the tree still carries it —
    so a reverse that blanked the marker would send an operator at a run they already made.
    """
    earlier = f"{'9' * 40} ansible/initial_setup.yml gitops_deploy"
    (state_dir / "broad_applied").write_text(earlier)
    config = plane_applies_radarr(settings, tick)
    tick.playbook_outcomes = [None, deploy_locks.ServiceLockBusy("lock sonarr busy")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "broad_applied") != PLANE_APPLIED
    assert marker(state_dir, "broad_applied") == earlier


def test_a_busy_service_lock_undoes_the_range_and_the_manual_plane_line(
    gitops_deploy, tick, settings, state_dir
):
    """Contention is not a failed deploy: nothing ran, so the whole range goes back.

    The `manual_plane` line this tick wrote goes back with it — the reset undoes the ff-merge,
    so the line would describe a range no tree carries.
    """
    config = mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy("service lock sonarr busy")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.head == LOCAL, "the ff-merge was undone"
    assert marker(state_dir, "hold_sha") is None
    assert marker(state_dir, "manual_plane") is None
    assert marker(state_dir, "contention_since").startswith(ORIGIN)
