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
    APPLY_GITOPS_DEPLOY,
    APPLYABLE_ROLE,
    DEPLOY_PLANE,
    DEPLOY_PLANE_FULL,
    DEPLOY_SONARR,
    LOCAL,
    ORIGIN,
    UNAPPLYABLE_ROLE,
    blocking,
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
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN


# ── the staging gate decides, before the ff-merge ─────────────────────────────────────────


def test_a_staging_rejection_defers_the_bump_and_still_applies_the_plane(
    gitops_deploy, tick, settings, state_dir
):
    """The gate is armed AND blocking on daniel-box, so this arm may not deploy past it.

    It demotes rather than holds: the range carries the setup plane and whatever else shared
    the push, and parking all of that behind one service's staging verdict is the cost
    `deploy_defer`'s `DECIDED:` measured. So the plane applies, the range merges, and the bump
    takes the defer-and-alert channel any unpromotable change takes.
    """
    config = blocking(settings, tick, APPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY], "the bump was not deployed"
    assert tick.merges == [ORIGIN], "the plane's range still merges"
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
    assert marker(state_dir, "hold_sha") is None, (
        "the range merged, so skip_hold could never match it again and the hold would stick"
    )


def test_the_staging_override_lets_the_bump_through(
    gitops_deploy, tick, settings, state_dir
):
    """The rejecting half: the documented one-tick escape hatch works here too.

    An override this arm ignored would be a trap — the operator arms one marker and two
    handlers read it.
    """
    config = blocking(settings, tick, APPLYABLE_ROLE)
    tick.staging_override = True
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY, DEPLOY_SONARR]
    assert not tick.staging_override, "the override is spent, not left armed"


def test_the_gate_is_consulted_before_the_ff_merge(gitops_deploy, tick, settings):
    """A death inside the gate's window must leave `local` behind origin, or the range strands."""
    config = mixed(settings, tick, APPLYABLE_ROLE)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert ("staging", {"sonarr"}) in tick.log, "the gate was never consulted"
    assert tick.log.index(("staging", {"sonarr"})) < tick.index("git", "merge")


def test_a_bump_the_deploy_plane_applies_never_reaches_the_gate(
    gitops_deploy, tick, settings, state_dir
):
    """A bump the narrowed deploy plane reaches is applied by that plane, ungated.

    Gating it would change nothing: a rejection cannot keep `deploy.yml --tags sonarr` from
    applying the pin, so the verdict post's "prod was not deployed" and the defer-and-alert
    post's "not applied" would both be false.
    """
    config = blocking(settings, tick, DEPLOY_PLANE)
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert not [entry for entry in tick.log if entry[0] == "staging"]
    assert marker(state_dir, "k8s_alerted_sha") is None, (
        "no post says it was not applied"
    )


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


@pytest.mark.parametrize(
    ("held", "cleared"),
    [("sonarr", True), ("radarr", False)],
    ids=["a-deploy-of-the-held-service", "an-unrelated-deploy"],
)
def test_a_bump_hold_clears_only_on_a_deploy_that_covers_it(
    gitops_deploy, tick, settings, state_dir, held, cleared
):
    """The way out of the hold above: a later `deploy.yml --tags sonarr` is the same apply.

    A k8s-only tick deploying sonarr applies exactly what the held run failed to, so it
    clears the hold; one deploying anything else is no evidence and must keep it.
    """
    (state_dir / "hold_sha").write_text("f" * 40)
    (state_dir / "hold_plane").write_text(f"ansible/deploy.yml {held}")
    config = mixed(settings, tick)
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [DEPLOY_SONARR]
    assert (marker(state_dir, "hold_sha") is None) is cleared
    assert (marker(state_dir, "hold_plane") is None) is cleared


def test_a_failed_bump_still_pages_the_k8s_change_beside_it(
    gitops_deploy, tick, settings, state_dir
):
    """A hand-edited k8s role in the same range is unapplied either way, and must say so.

    The failure arm used to return before `alert_deferred`, so radarr's only page was lost.
    """
    config = mixed(settings, tick, UNAPPLYABLE_ROLE)
    tick.paths = [*tick.paths, "ansible/roles/k8s/radarr/tasks/main.yml"]
    tick.playbook_outcomes = [RuntimeError("image manifest unknown")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
    assert any("radarr" in post for post in tick.posts)


def test_a_failed_broad_apply_never_reaches_the_bump(
    gitops_deploy, tick, settings, state_dir
):
    """The plane below has to succeed first, so its failure arm returns before the k8s deploy.

    The ff-merge already moved past the bump and the arm must not reset, so no later range
    carries it: the failure post is the only place left to name it.
    """
    config = mixed(settings, tick, APPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert tick.playbooks == [APPLY_GITOPS_DEPLOY]
    assert marker(state_dir, "hold_plane") == "ansible/initial_setup.yml gitops_deploy"
    assert "broad apply failed" in tick.posts[-1]
    assert "sonarr" in tick.posts[-1], "the dropped bump is named"


def test_a_failed_broad_apply_still_pages_a_demoted_bump(
    gitops_deploy, tick, settings, state_dir
):
    """A bump staging rejected sits in the defer-and-alert channel, which a failure skipped."""
    config = blocking(settings, tick, APPLYABLE_ROLE)
    tick.playbook_outcomes = [RuntimeError("the setup plane blew up")]
    assert gitops_deploy.main(tick.tools, config) == 0
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
    assert "broad apply failed" in tick.posts[-1]


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
    assert marker(state_dir, "k8s_alerted_sha") == ORIGIN
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
