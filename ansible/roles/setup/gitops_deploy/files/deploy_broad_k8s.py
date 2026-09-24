#!/usr/bin/env python3
"""The k8s half of a broad range: what the gate says about it, and applying it.

`split_k8s_auto_deploy` promotes an eligible image bump out of `ChangeSet.k8s` into
`k8s_deploy`, and it does that whether or not the range is broad. Until #2348 `handle_broad`
then ignored `k8s_deploy` entirely — so a mixed range fast-forwarded the bumps, deployed none
of them, and named none of them either, because `alert_deferred` fires its k8s channel on
`ChangeSet.k8s` alone. These two functions are that half of the broad arm: `_gate_broad_k8s`
decides whether it may deploy, `apply_broad_k8s` deploys it.

A module of its own rather than two more functions on `deploy_handlers.py`, which is at its
length cap — the same reason `deploy_staging_io.py` moved out of it in 2026-09. `handle_broad`
is the only production caller of either.

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
from deploy_tick_types import TickTarget
from deploy_toolbox import DeployTools


def gate_broad_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    cs: ChangeSet,
) -> ChangeSet:
    """Ask staging about the promoted bumps in a broad range, and demote them if it blocks.

    Returns:
        The ChangeSet the rest of the tick acts on. Unchanged when nothing was promoted or
        the verdict does not block; otherwise one whose `k8s_deploy` has been folded back
        into `k8s`, which is the defer-and-alert channel every unpromotable change takes.

    The gate is ARMED AND BLOCKING on daniel-box, the only host running this deployer
    (`gitops_deploy_staging_gate` and `_blocking`, both true in its host_vars since
    2026-09-02). Deploying the bumps in this arm without consulting it would be a way past an
    abort valve that a k8s-only tick honours, and it would leave a mixed range out of the tick
    ledger the Phase-C evidence is made of.

    DEMOTING RATHER THAN HOLDING is where this differs from `handle_k8s`, deliberately. That
    handler's range IS the bumps, so holding the SHA costs nothing; a broad range carries the
    setup plane and whatever else shared the push, and parking all of it behind one service's
    staging verdict is what `deploy_defer`'s `DECIDED:` measured the cost of. The verdict is
    about the gated services — `staging_scope` picks them — and says nothing about the setup
    plane, so the broad half still applies and the bumps defer-and-alert. No `hold_sha`
    either: the range merges below, so `skip_hold` could never match it again, and a hold
    nothing can clear turns GitOps Deploy — Status red for good.
    """
    if not cs.k8s_deploy:
        return cs
    origin = target.origin
    verdict = consult_staging(tools, state, config, cs.k8s_deploy, origin)
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
        f"{sorted(cs.k8s_deploy)} defer-and-alert rather than deploying"
    )
    return replace(cs, k8s=cs.k8s | cs.k8s_deploy, k8s_deploy=set())


def apply_broad_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    cs: ChangeSet,
    recorded: deploy_defer.Recorded,
    deadline: float,
) -> int | None:
    """Deploy the promoted image bumps that rode in on a broad range. Forward-only.

    Returns:
        None when the deploy succeeded or nothing was promoted, and the tick's exit code
        when it failed.

    Until #2348 `handle_broad` ended without this call and the promoted services were lost in
    SILENCE, not deferred. `split_k8s_auto_deploy` moves an eligible bump OUT of `cs.k8s` into
    `cs.k8s_deploy`, and `alert_deferred` fires its k8s channel on `cs.k8s` alone — so a broad
    tick fast-forwarded the bumps, deployed none of them and named none of them either. The
    01:45 tick on daniel-box on 2026-09-24 carried nine behind one `roles/setup/k3s` commit;
    the next tick's range began after them, and every one of the nine pods was still serving
    its old image when `kubectl` was read afterwards.

    FORWARD-ONLY, and not for a budget reason. `_rollback_k8s` resets the tree to `local`,
    which here would undo the ff-merge under a setup plane this tick already applied — the
    tree would claim the old commit while the host runs the new one, which is the exact state
    the broad arm's own no-reset rule exists to prevent.

    IT SHARES THE BROAD DEADLINE rather than taking `K8S_DEPLOY_TIMEOUT_S` of its own, for the
    reason the plans share it: `gitops-deploy.service.j2` sizes `TimeoutStartSec` treating this
    whole arm as one apply plus the staging pair and the flock wait, and a budget per phase
    would put a mixed range past that ceiling.
    """
    origin = target.origin
    if not cs.k8s_deploy:
        return None
    try:
        deploy_io.deploy_k8s(
            config.repo, cs.k8s_deploy, max(1.0, deadline - time.monotonic())
        )
    except deploy_locks.ServiceLockBusy as exc:
        # Same shape as the broad loop's arm above, and for the same reason: nothing here was
        # applied, the reset undoes the ff-merge, and the `manual_plane` lines this tick wrote
        # describe a range that is no longer merged. The broad apply that already ran is
        # idempotent, so the next tick re-crosses the whole range.
        deploy_defer.unrecord(state, origin, recorded)
        return deploy_defer.for_contention(tools, state, config, target, exc)
    except Exception as exc:
        log(f"k8s deploy failed for {sorted(cs.k8s_deploy)} on a broad tick: {exc}")
        state.write_hold(origin)
        # No `hold_plane`: that marker names a playbook and tag set for `clear_broad_hold` to
        # match an apply against, and an image bump is a service, not a plane. Writing one
        # here would leave a hold no broad apply can clear.
        posted = deploy_alerts.discord(
            tools,
            config,
            deploy_alerts.broad_k8s_failure_alert(
                config.hostname, origin, cs.k8s_deploy, exc, state.path("hold")
            ),
        )
        return 0 if posted else 1
    state.clear_service_hold()
    tools.emit_deploy_annotation(cs.k8s_deploy, origin)
    return None
