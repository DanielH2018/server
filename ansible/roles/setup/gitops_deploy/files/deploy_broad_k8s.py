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
) -> tuple[ChangeSet, set[str]]:
    """Ask staging about the promoted bumps in a broad range, and demote them if it blocks.

    Returns:
        The ChangeSet the rest of the tick acts on, and the bumps this gate demoted. The
        ChangeSet is unchanged and the set empty when nothing was gated or the verdict does
        not block; otherwise the gated bumps have been folded back into `k8s`, which is the
        defer-and-alert channel every unpromotable change takes. `handle_broad` records the
        demoted set in `k8s_deferred` at the ff-merge (`deploy_defer.record_demoted`) — it
        cannot be recorded here, because this runs BEFORE that merge and a contention arm
        below it resets the tree.

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
    covered = covered_by_plane(plans, cs.k8s_deploy)
    if covered:
        log(
            f"{sorted(covered)}: the deploy plane applies these, so staging is not asked"
        )
    gated = cs.k8s_deploy - covered
    if not gated:
        return cs, set()
    origin = target.origin
    verdict = consult_staging(tools, state, config, gated, origin)
    if not staging_blocks(verdict, blocking=config.staging_gate_blocking):
        return cs, set()
    if consume_staging_override(state):
        deploy_alerts.discord(
            tools,
            config,
            deploy_alerts.staging_override_alert(
                config.hostname, origin, state.path("staging_override")
            ),
        )
        log(f"staging rejected {origin[:8]}; override armed, deploying prod anyway")
        return cs, set()
    log(
        f"staging rejected {origin[:8]}; the broad range still merges and applies, and "
        f"{sorted(gated)} defer-and-alert rather than deploying"
    )
    return replace(cs, k8s=cs.k8s | gated, k8s_deploy=cs.k8s_deploy - gated), gated


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
    defer-and-alert post names it once, and the `k8s_deferred` marker keeps naming it until
    something deploys it (#2449). `Release Staleness Drift` reads the unapplied pin from the
    release records too, and is not enough on its own — that monitor is DOWN for any stale
    record anywhere in the fleet, so a new deferral adds nothing visible to a tile that is
    already red.
    """
    origin = target.origin
    bumps = cs.k8s_deploy - covered_by_plane(plans, cs.k8s_deploy)
    # A k8s role the deploy plane APPLIED is not a deferred change, whatever the post would
    # otherwise say (#2453). `narrow_broad` maps a role's own changed path to its tag, so the
    # narrowed list names it whenever the range also carries a deploy-plane path, and a refused
    # narrowing runs the whole play — either way the probe that measured this ran `deploy.yml
    # --tags radarr,sonarr` and then posted "fast-forwarded but not applied" for the same
    # roles, printing that same command as the remedy. Every plan here succeeded: this runs
    # after the loop, whose failure and contention arms both return.
    #
    # INTERSECTED WITH THE DECLARED ENTRIES, which `covered_by_plane` alone does not do. It
    # returns the WHOLE set on a refused narrowing, and that is sound for `k8s_deploy` (a
    # promoted bump is declared by construction) but not for `cs.k8s`, which `_ACTIVE_K8S`
    # fills from role directories in the tree. `deploy.yml` applies no role this host does not
    # declare — the same fact `k8s_remediation` prescribes a full deploy for — so subtracting
    # one would page nowhere at all. A shared role a declared role calls IS applied by a full
    # run and still stays in the post: that is the pre-existing false "not applied", and it is
    # the safe side of the two.
    plane_applied = covered_by_plane(plans, cs.k8s) & plan.k8s_services
    if plane_applied:
        log(
            f"{sorted(plane_applied)}: the deploy plane applied these, so they are not deferred"
        )
        cs = replace(cs, k8s=cs.k8s - plane_applied)
    # A bump an EARLIER tick deferred for budget, applied by this tick's deploy plane, is no
    # longer owed. The pending set is asked rather than the range: the deferring tick merged
    # the bump, so it is in nobody's `local..origin` any more (#2449).
    deploy_defer.clear_applied_k8s_deferred(
        state, covered_by_plane(plans, deploy_defer.pending_k8s_deferred(state))
    )
    if bumps and deadline - time.monotonic() < config.k8s_deploy_timeout_s:
        log(
            f"{sorted(bumps)}: {deadline - time.monotonic():.0f}s of the broad budget left, "
            f"under the {config.k8s_deploy_timeout_s}s a bump gets — deferring, not deploying"
        )
        cs = replace(cs, k8s=cs.k8s | bumps, k8s_deploy=cs.k8s_deploy - bumps)
        # The durable half of the deferral (#2449). The post below names these once, and the
        # range is already merged — no later tick's `local..origin` carries the bump again, so
        # without a marker the only thing still reporting it is `Release Staleness Drift`
        # reading the unapplied pin, which a stale record elsewhere can park DOWN for reasons
        # of its own. The marker pages GitOps Deploy — Status on its own age instead, and the
        # next deploy of the service clears it.
        #
        # Safe to write here rather than on an exit: this branch empties `bumps`, so the
        # deploy below is skipped and neither the contention arm nor the failure arm that
        # would have to take the marker back is reachable from it.
        state.record_k8s_deferred(origin, bumps, time.time())
        bumps = set()
    # The deploy plane has already applied these, so they are annotated on every path out of
    # here BUT the reset one — a failed bump beside them must not hide that they went out.
    # Not before the deploy below, which is where they were annotated until #2453: a busy lock
    # there resets the tree, the next tick re-applies the same plane, and Grafana drew a second
    # annotation for services that deployed once.
    applied = cs.k8s_deploy - bumps
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
            # The plane is the run that failed, `deploy.yml --tags <bumps>`, ADDED to any plane
            # already held. Without it any later service deploy cleared the hold
            # (`clear_service_hold`); written over an earlier entry, the bump's fix-forward
            # cleared a plane still unapplied. Either way Status went green too early.
            state.hold_failed_apply(origin, DEPLOY_PLAYBOOK, sorted(bumps))
            if applied:
                tools.emit_deploy_annotation(applied, origin)
            # The range is merged either way, so a hand-edited or denylisted k8s role in it has
            # no page but this one. Before the failure post, which stays the tick's last word.
            # The secrets page rides this exit for the reason the `DECIDED:` in
            # `deploy_handlers.handle_broad`'s own failure arm gives (#2459).
            deploy_alerts.alert_deferred(
                tools, state, config, origin, applied, cs, plan.k8s_services
            )
            deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
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
        deploy_defer.clear_applied_k8s_deferred(state, bumps)
    if applied:
        tools.emit_deploy_annotation(applied, origin)
    if bumps:
        tools.emit_deploy_annotation(bumps, origin)
    deploy_alerts.alert_deferred(
        tools, state, config, origin, cs.k8s_deploy, cs, plan.k8s_services
    )
    # The tick's last exit, and the only one a range with nothing deferred reaches, so the
    # secrets page has to be here as well as on the two failure arms (#2459).
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    return 0
