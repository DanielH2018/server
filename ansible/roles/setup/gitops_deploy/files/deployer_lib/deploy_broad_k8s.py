#!/usr/bin/env python3
"""The k8s half of a broad range: deploying the promoted bumps a plane did not.

`split_k8s_auto_deploy` promotes an eligible image bump out of `ChangeSet.k8s` into
`k8s_deploy`, and it does that whether or not the range is broad. Until #2348 `handle_broad`
then ignored `k8s_deploy` entirely — so a mixed range fast-forwarded the bumps, deployed none
of them, and named none of them either, because `alert_deferred` fires its k8s channel on
`ChangeSet.k8s` alone. These functions are that half of the broad arm: `apply_broad_k8s`
deploys it, and `covered_by_plane` says which bumps the deploy plane applies on its own.

A module of its own because it answers, from its arguments and the toolbox, which k8s roles
a broad range deploys beside its planes. It reads nothing private to `deploy_handlers.py`.
`handle_broad` there calls the bump functions, and `main()` calls `route_secret_k8s_readers`
after `plan_tick`. That one is the other k8s half (#5577): the roles reading a `secrets.yml`
key a range moved, when no deploy-plane path sends the range to `narrow`. `handle_broad`
appends the plan it returns after its own.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import time
from dataclasses import replace

from deployer_lib import deploy_alert_text
from deployer_lib import deploy_alerts
from deployer_lib import deploy_defer
from deployer_lib import deploy_io
from deployer_lib import deploy_k8s_owed
from deployer_lib import deploy_locks
from deployer_lib.deploy_changes import ChangeSet
from deployer_lib.deploy_config import ERR, WARNING, Config, log
from deployer_lib.deploy_narrow import BroadPlan, denylisted_in
from deployer_lib.deploy_setup_roles import SECRET_READERS_REFUSED_EXIT
from deployer_lib.deploy_state import DeployerState
from deployer_lib.deploy_tick_types import TickPlan, TickTarget
from deployer_lib.deploy_toolbox import DeployTools

# The playbook a promoted bump is deployed through, and the one the deploy plane runs.
from gitops_markers import DEPLOY_PLAYBOOK


def covered_by_plane(plans, bumps: set[str]) -> set[str]:
    """The bumps a deploy-plane plan in `plans` applies on its own.

    `narrow_lib/broad.py:_changed_half` builds its ChangeSet from the raw paths, so a bump's tag
    lands in the narrowed list whenever the range also carries a deploy-plane path.
    Deploying those bumps again re-took the Longhorn snapshot of every claim they declare
    and spent the shared budget twice. A plan that applies nothing (`narrowed-to-nothing`, or
    a refused narrowing deferred since #4333) covers nothing.
    """
    for broad in plans:
        if broad.playbook == DEPLOY_PLAYBOOK and broad.apply:
            return set(bumps) & set(broad.tags)
    return set()


def deferred_owed(broad, declared: set[str]) -> set[str]:
    """The services a deferred deploy plane owes; empty for a plan that was not deferred.

    The refusal's own tags when it named them, else every declared service, because the
    derivation could rule none out. Intersected with `declared`, the entries this host
    deploys: a Pi service has no deploy here to discharge its line. `fold_deferred` records
    this set in the ledger and `record_deferred_receipt` in the range's receipt, so the two
    agree.
    """
    if not broad.deferred:
        return set()
    return (set(broad.owed) or set(declared)) & declared


def record_deferred_receipt(
    state: DeployerState, target: TickTarget, plans, declared: set[str]
) -> None:
    """Record each deferred plane's owed services under the range receipt's `deferred` key.

    land.sh reads that entry as discharged once none of its services holds a ledger line
    (#4843), so the entry must never exist before its lines do. Every exit that leaves the
    range merged runs this after `deploy_k8s_owed.alert_and_record_deferred` has written
    them. Written from the plans loop, it preceded the bump deploy below by minutes, and a
    landing that read the markers in that window counted 0 owed and settled (#4866).

    A plane that owes no declared service writes nothing (#4865). An empty entry would count
    0 owed at once and read as applied, for a plane nothing ran: an unreadable host_vars
    empties `declared`, and a refusal can name only services this host does not deploy.
    """
    for broad in plans:
        owed = deferred_owed(broad, declared)
        if owed:
            state.record_receipt(
                target.origin, target.local, deferred={broad.playbook: sorted(owed)}
            )


def fold_deferred(cs: ChangeSet, plans, declared: set[str]) -> ChangeSet:
    """`cs` with the services a deferred deploy plane owes added to `cs.k8s` (#4333).

    A refused narrowing applies nothing, so what it may reach joins the k8s roles this tick
    merged and did not apply, and `alert_and_record_deferred` records and pages them as one
    set.
    """
    owed: set[str] = set()
    for broad in plans:
        owed |= deferred_owed(broad, declared)
    if not owed:
        return cs
    log(
        f"k8s_unapplied: {len(owed)} services deferred here: the deploy plane could not be "
        "narrowed, and the whole play does not fit the tick's budget"
    )
    return replace(cs, k8s=cs.k8s | owed)


def route_secret_k8s_readers(
    tools: DeployTools, config: Config, target: TickTarget, plan: TickPlan
) -> TickPlan:
    """Give a `secrets.yml` range with no deploy-plane path a plan for its k8s readers (#5577).

    `narrow` maps each moved key to the k8s roles reading it (#5409), but `handle_broad` asks
    it only when the range carries a deploy-plane path. A rotation alone set `cs.secrets` and
    nothing else, so it merged, paged, and every reader kept the old value until a hand
    redeployed it. This asks `secret_setup_readers.py --deploy-tags` for the secrets half
    alone, as `deploy_defer.route_secret_readers` asks it for the setup roles, and routes the
    range through `handle_broad` only when a reader exists. A range with a deploy-plane path
    is skipped, because `narrow` already maps its keys, and so is a range carrying any k8s
    role change, for the reason the `DECIDED:` below gives.

    The tags it applies drop each one Flux owns, as `deploy_narrow._deploy_plane` does. A
    refusal naming its tags (the fleet-coverage ceiling) defers them to `k8s_unapplied`.

    `config.k8s_autodeploy_denylist` does not subtract from the tags either (#5634). This is
    the broad plane, and the `DECIDED: the broad plane ignores K8S_AUTODEPLOY_DENYLIST` on
    `deploy_narrow.denylisted_in` covers it: the denylist keeps a role out of the snapshot
    and the automatic rollback, and this plan has neither. It applies the reader forward-only,
    as `deploy.sh --tags <role>` would. The journal names each denied tag it applies.
    """
    cs = plan.cs
    if not cs.secrets or cs.broad_deploy:
        return plan
    # DECIDED: a range that also carries a k8s role change or a promoted image bump is left
    # alone, and merges and pages as before #5577. A secret plan applies each reader's whole
    # render, so a reader whose template the range edits (the /add-secret shape) would ship
    # that edit forward-only, past the defer-and-alert every hand-edited k8s change takes.
    # `cs.broad` would also move a promoted bump from `handle_k8s`, with its snapshot and
    # rollback, to the forward-only broad arm. The rotation alone is the case this route is for.
    if cs.k8s or cs.k8s_deploy or cs.k8s_consumers:
        log(
            "secret-readers: the range also changes a k8s role, so no k8s reader is routed; "
            "the secrets page names the consumers"
        )
        return plan
    # DECIDED: a failed or nameless lookup merges and pages, where the setup half parks
    # (#5436). The `secrets` page goes out on every merged exit and names the command listing
    # each consumer, which was this range's only signal before #5577. A park on a failure
    # that repeats, such as a checkout older than this deployer whose script lacks the flag
    # (argparse exits 64), would hold every other session's landing behind a rotation.
    try:
        rc, out = tools.secret_k8s_readers(config.repo, target.local, target.origin)
    except Exception as exc:
        log(
            f"secret-readers: k8s lookup failed ({type(exc).__name__}: {exc}) — no k8s "
            "reader routed; the secrets page names the consumers",
            level=WARNING,
        )
        return plan
    tags = sorted({t for t in out.split(",") if t})
    if rc == SECRET_READERS_REFUSED_EXIT and tags:
        log(
            f"secret-readers: cannot narrow the k8s readers ({','.join(tags)}) — "
            "deferring them to k8s_unapplied"
        )
        secret_plan = BroadPlan(
            DEPLOY_PLAYBOOK, [], False, deferred=True, owed=tuple(tags)
        )
    elif rc != 0:
        log(
            f"secret-readers: no k8s reader routed (exit {rc}); the secrets page names the "
            "consumers"
        )
        return plan
    else:
        for t in (t for t in tags if t in plan.owners):
            log(
                f"secret-readers: dropping {t}: owned by {plan.owners[t]}, so Flux applies it"
            )
        tags = [t for t in tags if t not in plan.owners]
        if not tags:
            return plan
        log(
            f"secret-readers: secrets.yml moved a key {','.join(tags)} read — applying "
            f"{DEPLOY_PLAYBOOK} --tags {','.join(tags)}"
        )
        denied = denylisted_in(tags, config.k8s_autodeploy_denylist)
        if denied:
            log(
                f"secret-readers: {','.join(denied)} are denylisted for k8s auto-deploy and "
                "are applied here regardless — this plan runs the plain playbook "
                "forward-only, with no snapshot or rollback for the denylist to withhold"
            )
        secret_plan = BroadPlan(DEPLOY_PLAYBOOK, tags, True)
    cs.broad = True
    return replace(plan, secret_plans=(secret_plan,))


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
    this whole arm as one apply plus the flock wait. It STARTS only when
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
    cs = fold_deferred(cs, plans, plan.k8s_services)
    bumps = cs.k8s_deploy - covered_by_plane(plans, cs.k8s_deploy)
    # A k8s role the deploy plane APPLIED is not a deferred change, whatever the post would
    # otherwise say (#2453). `narrow_lib/broad.py` maps a role's own changed path to its tag, so the
    # narrowed list names it whenever the range also carries a deploy-plane path — the probe
    # that measured this ran `deploy.yml --tags radarr,sonarr` and then posted
    # "fast-forwarded but not applied" for the same roles, printing that same command as the
    # remedy. Every plan here succeeded: this runs after the loop, whose failure and
    # contention arms both return.
    #
    # INTERSECTED WITH THE DECLARED ENTRIES: `deploy.yml` applies no role this host does not
    # declare, so a role `role_of` found in the tree but no entry names stays in the post.
    plane_applied = covered_by_plane(plans, cs.k8s) & plan.k8s_services
    if plane_applied:
        log(
            f"broad-k8s: {sorted(plane_applied)}: the deploy plane applied these, so they are not deferred"
        )
        cs = replace(cs, k8s=cs.k8s - plane_applied)
    # A bump an EARLIER tick deferred for budget, applied by this tick's deploy plane, is no
    # longer owed. The pending set is asked rather than the range: the deferring tick merged
    # the bump, so it is in nobody's `local..origin` any more (#2449).
    deploy_k8s_owed.clear_applied_k8s_deferred(
        state, covered_by_plane(plans, deploy_k8s_owed.pending_k8s_deferred(state))
    )
    if bumps and deadline - time.monotonic() < config.k8s_deploy_timeout_s:
        log(
            f"broad-k8s: {sorted(bumps)}: {deadline - time.monotonic():.0f}s of the broad budget left, "
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
        deploy_k8s_owed.defer_bumps(state, origin, bumps)
        bumps = set()
    # The deploy plane has already applied these, so they are annotated on every path out of
    # here BUT the reset one — a failed bump beside them must not hide that they went out.
    # Not before the deploy below, which is where they were annotated until #2453: a busy lock
    # there resets the tree, the next tick re-applies the same plane, and Grafana drew a second
    # annotation for services that deployed once.
    applied = cs.k8s_deploy - bumps
    if bumps:
        try:
            deploy_io.deploy_k8s(
                config.repo, bumps, deadline - time.monotonic(), run=tools.run
            )
        except deploy_locks.ServiceLockBusy as exc:
            # Same shape as the broad loop's arm, and for the same reason: nothing here was
            # applied, the reset undoes the ff-merge, and the `manual_plane` lines this tick
            # wrote describe a range that is no longer merged. The broad apply that already ran
            # is idempotent, so the next tick re-crosses the whole range — which is also why no
            # deferred-change page goes out here: that tick sends it.
            deploy_defer.unrecord(state, origin, recorded)
            return deploy_defer.for_contention(tools, state, config, target, exc)
        except Exception as exc:
            log(
                f"broad-k8s: k8s deploy failed for {sorted(bumps)} on a broad tick: {exc}",
                level=ERR,
            )
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
            deploy_k8s_owed.alert_and_record_deferred(
                tools, state, config, origin, applied, cs, plan.k8s_services
            )
            record_deferred_receipt(state, target, plans, plan.k8s_services)
            deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
            posted = deploy_alerts.discord(
                tools,
                config,
                deploy_alert_text.broad_k8s_failure_alert(
                    config.hostname,
                    origin,
                    bumps,
                    exc,
                    state.path("hold"),
                ),
            )
            return 0 if posted else 1
        state.clear_service_hold(bumps)
        deploy_k8s_owed.clear_applied_k8s_deferred(state, bumps)
    if applied:
        tools.emit_deploy_annotation(applied, origin)
    if bumps:
        tools.emit_deploy_annotation(bumps, origin)
    deploy_k8s_owed.alert_and_record_deferred(
        tools, state, config, origin, cs.k8s_deploy, cs, plan.k8s_services
    )
    record_deferred_receipt(state, target, plans, plan.k8s_services)
    # The tick's last exit, and the only one a range with nothing deferred reaches, so the
    # secrets page has to be here as well as on the two failure arms (#2459).
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    return 0
