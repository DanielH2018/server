#!/usr/bin/env python3
"""A busy service lock defers the tick; every other failure still holds and rolls back.

ADR-0017 gave this deployer one lock per service, and an operator `./scripts/deploy.sh` can be
holding one when a tick wants it. That is contention, not a failed deploy: nothing ran, so
holding the SHA would park every later tick behind a lock that has since been released, and
rolling back would redeploy a version that is already live.

Each handler is driven directly, the way `test_gitops_deploy_phases.py` drives them, because
what is under test is one `except` arm rather than a whole tick. The FLAGGED half of each pair
— the ordinary failure that must still hold and roll back — lives in
`test_gitops_deploy_main_branches.py`, which runs those same handlers through `main()`.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_lock_contention.py
"""

import dataclasses

import deploy_handlers
import deploy_locks
import deploy_tick_types
from deploy_changes import ChangeSet

LOCAL = "1" * 40
ORIGIN = "2" * 40
BUSY = "service lock sonarr busy for 900s"


def _plan(cs: ChangeSet, paths=None) -> deploy_tick_types.TickPlan:
    return deploy_tick_types.TickPlan(
        cs=cs, paths=list(paths or []), k8s_services=set()
    )


def _target(**overrides) -> deploy_tick_types.TickTarget:
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


def _busy(tick) -> None:
    tick.playbook_outcomes = [deploy_locks.ServiceLockBusy(BUSY)]


def _deferred(tick, state_dir, posts: int = 0) -> None:
    """What every contention branch must leave behind, whichever handler took it.

    `posts` is what the tick had ALREADY sent before it reached the deploy — the manual_plane
    page of a mixed range is the only one. The contention arm itself never pages.
    """
    assert tick.head == LOCAL, (
        "the ff-merge was not undone, so the next tick sees no range"
    )
    assert not (state_dir / "hold_sha").exists(), "contention must not hold the SHA"
    assert not (state_dir / "hold_plane").exists(), "contention must not hold a plane"
    assert len(tick.posts) == posts, "contention is not a page; the next tick retries"


def test_a_busy_service_lock_defers_a_k8s_deploy(
    gitops_deploy, tick, settings, state_dir
):
    """CLEAN half for handle_k8s: one rollout is running, so this tick does nothing at all.

    The rollback the FLAGGED half takes would revert volumes to a snapshot and redeploy the
    prior pin over a cluster this tick never touched — strictly more dangerous than waiting.
    """
    _busy(tick)
    plan = _plan(ChangeSet(k8s_deploy={"sonarr"}))
    code = deploy_handlers.handle_k8s(
        tick.tools, gitops_deploy.STATE, settings, _target(), plan
    )
    assert code == 0
    assert len(tick.playbooks) == 1, "a rollback ran for a deploy that never started"
    _deferred(tick, state_dir)


def test_a_busy_service_lock_defers_a_docker_deploy(
    gitops_deploy, tick, settings, state_dir
):
    """CLEAN half for handle_docker: the health gate is never reached either."""
    _busy(tick)
    tick.declare("containers_list:\n  - name: wg-easy\n    platform: docker\n")
    tick.render("wg-easy")
    plan = _plan(ChangeSet(services={"wg-easy"}))
    code = deploy_handlers.handle_docker(
        tick.tools, gitops_deploy.STATE, settings, _target(), plan
    )
    assert code == 0
    assert len(tick.playbooks) == 1, "the prior version was redeployed over nothing"
    _deferred(tick, state_dir)


def test_a_busy_service_lock_defers_a_broad_apply(
    gitops_deploy, tick, settings, state_dir
):
    """CLEAN half for handle_broad, which is forward-only and so has no rollback to skip.

    What it must skip is `hold_plane`: a plane that was never applied is not a plane to hold,
    and a hold there parks the Renovate agent and pages the Status monitor until a hand clears
    both markers.
    """
    _busy(tick)
    plan = _plan(
        ChangeSet(broad=True, broad_setup=True, setup_roles={"gitops_deploy"}),
        paths=["ansible/roles/setup/gitops_deploy/templates/config.env.j2"],
    )
    code = deploy_handlers.handle_broad(
        tick.tools, gitops_deploy.STATE, settings, _target(), plan
    )
    assert code == 0
    _deferred(tick, state_dir)
    assert not (state_dir / "broad_applied").exists(), (
        "an apply that never ran was recorded as one; land.sh reads this to believe a plane "
        "is live"
    )


# A range carrying BOTH an applyable setup role and one no playbook here can apply. The second
# is what `manual_plane` records, and a record means merged-and-unapplied — so a tick that
# undoes its own merge must take the record back with it.
_MIXED = ChangeSet(
    broad=True,
    broad_setup=True,
    setup_roles={"gitops_deploy", "k3s"},
)
_MIXED_PATHS = [
    "ansible/roles/setup/gitops_deploy/templates/config.env.j2",
    "ansible/roles/setup/k3s/tasks/main.yml",
]


def test_a_contended_mixed_range_records_no_pending_role(
    gitops_deploy, tick, settings, state_dir
):
    """CLEAN half: nothing is merged when this returns, so nothing is pending.

    `k3s` is applied by `k3s-bringup.yml`, which this deployer never runs, so the range records
    it and pages. Left behind after the reset it describes a tree nobody has — and
    monitor-bridge pages on its age for six hours whatever the tree says.
    """
    _busy(tick)
    code = deploy_handlers.handle_broad(
        tick.tools,
        gitops_deploy.STATE,
        settings,
        _target(),
        _plan(_MIXED, _MIXED_PATHS),
    )
    assert code == 0
    assert not (state_dir / "manual_plane").exists(), (
        "a role was left recorded as merged-and-unapplied for a merge that was undone"
    )
    assert not (state_dir / "broad_alerted").exists(), (
        "the page dedupe survived, so the record this range gets next tick would be silent"
    )
    # One post, and it is `record`'s own — sent before the apply was even attempted, so it
    # cannot be recalled. Clearing the dedupe above is what makes the next tick page again for
    # the range once it really is merged; until then the message's own advice still holds.
    _deferred(tick, state_dir, posts=1)


def test_a_failed_mixed_apply_still_records_its_pending_role(
    gitops_deploy, tick, settings, state_dir
):
    """FLAGGED half, and the property #1773 bought: a FAILED apply keeps the record.

    The tree stays fast-forwarded there, so the role really is merged and unapplied. Undoing
    the record on that path would reopen the hole the marker exists to close — the role sits on
    disk with no marker, and once the operator fixes forward past the held SHA the range never
    comes back.
    """
    tick.playbook_outcomes = [RuntimeError("the play failed")]
    code = deploy_handlers.handle_broad(
        tick.tools,
        gitops_deploy.STATE,
        settings,
        _target(),
        _plan(_MIXED, _MIXED_PATHS),
    )
    assert code == 0
    assert "k3s" in (state_dir / "manual_plane").read_text()
    assert tick.head == ORIGIN, (
        "a failed broad apply must not reset; it is forward-only"
    )
