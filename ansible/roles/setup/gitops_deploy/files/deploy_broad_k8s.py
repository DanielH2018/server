#!/usr/bin/env python3
"""The k8s half of a broad range: what the gate says about it, and applying it.

`split_k8s_auto_deploy` promotes an eligible image bump out of `ChangeSet.k8s` into
`k8s_deploy`, and it does that whether or not the range is broad. Until #2348 `handle_broad`
then ignored `k8s_deploy` entirely — so a mixed range fast-forwarded the bumps, deployed none
of them, and named none of them either, because `alert_deferred` fires its k8s channel on
`ChangeSet.k8s` alone. These functions are that half of the broad arm: `gate_broad_k8s`
decides whether it may deploy, `apply_broad_k8s` deploys it, and `covered_by_plane` says which
bumps the deploy plane applies on its own.

A module of its own rather than more functions on `deploy_handlers.py`, which is at its
length cap — the same reason `deploy_staging_io.py` moved out of it in 2026-09. `handle_broad`
is the only production caller of these.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import time
from dataclasses import replace

import deploy_alerts
import deploy_defer
import deploy_io
import deploy_locks
from deploy_changes import ChangeSet
from deploy_config import Config, log
from deploy_git import hold_plane_marker
from deploy_staging import staging_blocks
from deploy_staging_io import consult_staging, consume_staging_override
from deploy_state import DeployerState
from deploy_tick_types import TickPlan, TickTarget
from deploy_toolbox import DeployTools

# The playbook a promoted bump is deployed through, and the one the deploy plane runs.
DEPLOY_PLAYBOOK = "ansible/deploy.yml"


def covered_by_plane(plans, bumps: set[str]) -> set[str]:
    """The bumps a deploy-plane plan in `plans` applies on its own.

    `narrow_broad._changed_half` builds its ChangeSet from the raw paths, so a bump's tag
    lands in the narrowed list whenever the range also carries a deploy-plane path, and a
    refused narrowing runs the whole play. Deploying those bumps again re-took the Longhorn
    snapshot of every claim they declare and spent the shared budget twice. A plan that
    applies nothing (`narrowed-to-nothing`) covers nothing.
    """
    for broad in plans:
        if broad.playbook == DEPLOY_PLAYBOOK and broad.apply:
            return set(bumps) if not broad.tags else set(bumps) & set(broad.tags)
    return set()


def gate_broad_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    cs: ChangeSet,
    plans,
) -> ChangeSet:
    """Ask staging about the promoted bumps in a broad range, and demote them if it blocks.

    Returns:
        The ChangeSet the rest of the tick acts on. Unchanged when nothing was gated or the
        verdict does not block; otherwise one whose gated bumps have been folded back into
        `k8s`, which is the defer-and-alert channel every unpromotable change takes.

    The gate is ARMED AND BLOCKING on daniel-box, the only host running this deployer
    (`gitops_deploy_staging_gate` and `_blocking`, both true in its host_vars since
    2026-09-02). Deploying the bumps in this arm without consulting it would be a way past an
    abort valve that a k8s-only tick honours, and it would leave a mixed range out of the tick
    ledger the Phase-C evidence is made of.

    A BUMP THE DEPLOY PLANE COVERS IS NOT GATED. The plane applies it whatever the verdict,
    exactly as it applied every such bump before #2348, so a rejection could withhold nothing:
    it would only make the verdict post's "prod was not deployed" and the defer-and-alert
    post's "not applied" false.

    DEMOTING RATHER THAN HOLDING is where this differs from `handle_k8s`, deliberately. That
    handler's range IS the bumps, so holding the SHA costs nothing; a broad range carries the
    setup plane and whatever else shared the push, and parking all of it behind one service's
    staging verdict is what `deploy_defer`'s `DECIDED:` measured the cost of. The verdict is
    about the gated services — `staging_scope` picks them — and says nothing about the setup
    plane, so the broad half still applies and the bumps defer-and-alert. No `hold_sha`
    either: the range merges below, so `skip_hold` could never match it again, and a hold
    nothing can clear turns GitOps Deploy — Status red for good.
    """
    gated = cs.k8s_deploy - covered_by_plane(plans, cs.k8s_deploy)
    if not gated:
        return cs
    origin = target.origin
    verdict = consult_staging(tools, state, config, gated, origin)
    if not staging_blocks(verdict, blocking=config.staging_gate_blocking):
        return cs
    if consume_staging_override(state):
        deploy_alerts.discord(
            tools,
            config,
            deploy_alerts.staging_override_alert(
                config.hostname, origin, state.path("staging_override")
            ),
        )
        log(f"staging rejected {origin[:8]}; override armed, deploying prod anyway")
        return cs
    log(
        f"staging rejected {origin[:8]}; the broad range still merges and applies, and "
        f"{sorted(gated)} defer-and-alert rather than deploying"
    )
    return replace(cs, k8s=cs.k8s | gated, k8s_deploy=cs.k8s_deploy - gated)


def apply_broad_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
    cs: ChangeSet,
    plans,
    recorded: deploy_defer.Recorded,
    deadline: float,
) -> int:
    """Deploy the promoted bumps the planes did not, then fire the deferred-change pages.

    Returns:
        The tick's exit code.

    Until #2348 `handle_broad` ended without this call and the promoted services were lost in
    SILENCE, not deferred. The 01:45 tick on daniel-box on 2026-09-24 carried nine behind one
    `roles/setup/k3s` commit; the next tick's range began after them, and every one of the
    nine pods was still serving its old image when `kubectl` was read afterwards.

    FORWARD-ONLY, and not for a budget reason. `_rollback_k8s` resets the tree to `local`,
    which here would undo the ff-merge under a setup plane this tick already applied — the
    tree would claim the old commit while the host runs the new one, which is the exact state
    the broad arm's own no-reset rule exists to prevent.

    IT SHARES THE BROAD DEADLINE rather than adding a `K8S_DEPLOY_TIMEOUT_S` of its own, for
    the reason the plans share it: `gitops-deploy.service.j2` sizes `TimeoutStartSec` treating
    this whole arm as one apply plus the staging pair and the flock wait. It STARTS only when
    what is left is at least `k8s_deploy_timeout_s`, the budget a k8s-only tick grants the
    same bump. With less, the run would die at the timeout (a hold and a page, often for a
    service that was healthy) or its lock wait would raise `ServiceLockBusy` and reset the
    tree under planes that applied. So it defers instead: the bump joins `cs.k8s`, the
    defer-and-alert post names it, and `Release Staleness Drift` reads the unapplied pin from
    the release records until something deploys it.
    """
    origin = target.origin
    bumps = cs.k8s_deploy - covered_by_plane(plans, cs.k8s_deploy)
    if bumps and deadline - time.monotonic() < config.k8s_deploy_timeout_s:
        log(
            f"{sorted(bumps)}: {deadline - time.monotonic():.0f}s of the broad budget left, "
            f"under the {config.k8s_deploy_timeout_s}s a bump gets — deferring, not deploying"
        )
        cs = replace(cs, k8s=cs.k8s | bumps, k8s_deploy=cs.k8s_deploy - bumps)
        bumps = set()
    if bumps:
        try:
            deploy_io.deploy_k8s(config.repo, bumps, deadline - time.monotonic())
        except deploy_locks.ServiceLockBusy as exc:
            # Same shape as the broad loop's arm, and for the same reason: nothing here was
            # applied, the reset undoes the ff-merge, and the `manual_plane` lines this tick
            # wrote describe a range that is no longer merged. The broad apply that already ran
            # is idempotent, so the next tick re-crosses the whole range — which is also why no
            # deferred-change page goes out here: that tick sends it.
            deploy_defer.unrecord(state, origin, recorded)
            return deploy_defer.for_contention(tools, state, config, target, exc)
        except Exception as exc:
            log(f"k8s deploy failed for {sorted(bumps)} on a broad tick: {exc}")
            state.write_hold(origin)
            # The plane is the run that failed, `deploy.yml --tags <bumps>`. Without it any
            # later service deploy cleared the hold (`clear_service_hold`), and GitOps Deploy —
            # Status went green over the failed pin; with it, only a run covering these tags does.
            state.write("hold_plane", hold_plane_marker(DEPLOY_PLAYBOOK, sorted(bumps)))
            # The range is merged either way, so a hand-edited or denylisted k8s role in it has
            # no page but this one. Before the failure post, which stays the tick's last word.
            deploy_alerts.alert_deferred(
                tools, state, config, origin, set(), cs, plan.k8s_services
            )
            posted = deploy_alerts.discord(
                tools,
                config,
                deploy_alerts.broad_k8s_failure_alert(
                    config.hostname,
                    origin,
                    bumps,
                    exc,
                    state.path("hold"),
                    state.path("hold_plane"),
                ),
            )
            return 0 if posted else 1
        state.clear_service_hold(bumps)
    if cs.k8s_deploy:
        tools.emit_deploy_annotation(cs.k8s_deploy, origin)
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    deploy_alerts.alert_deferred(
        tools, state, config, origin, cs.k8s_deploy, cs, plan.k8s_services
    )
    return 0
