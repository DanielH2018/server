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


def _deferred(tick, state_dir) -> None:
    """What every contention branch must leave behind, whichever handler took it."""
    assert tick.head == LOCAL, (
        "the ff-merge was not undone, so the next tick sees no range"
    )
    assert not (state_dir / "hold_sha").exists(), "contention must not hold the SHA"
    assert not (state_dir / "hold_plane").exists(), "contention must not hold a plane"
    assert tick.posts == [], "contention is not a page; the next tick retries"


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
