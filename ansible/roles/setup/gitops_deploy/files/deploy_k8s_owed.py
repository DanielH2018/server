#!/usr/bin/env python3
"""The k8s changes the deployer owes: the `k8s_deferred` and `k8s_unapplied` ledger classes (#3669).

`k8s_deferred` holds the promoted image bumps a broad tick merged and had no budget left to
deploy (#2449); monitor-bridge pages on its age. `k8s_unapplied` holds the k8s role changes a
tick merged and never applies, because the role is denylisted or the change is not a bump
(#2570); nothing pages on it. This module is the deployer's one reader and writer of both:

- `defer_bumps` and `alert_and_record_deferred` record,
- `clear_applied_k8s_deferred`, `discharge_k8s_unapplied` and `drop_deleted_k8s_unapplied`
  discharge,
- `pending_k8s_deferred` answers what is owed,
- `reconcile` is the tick-start pass that discharges and names what is left.

Within the deployer's `files/`, no other module reads or writes the two classes
(`tests/test_k8s_unapplied_marker.py::test_only_deploy_k8s_owed_touches_the_k8s_ledger_classes`).
`deploy_state.DeployerState`'s owed methods stay the storage layer under it, because
`scripts/deploy_tools/gitops_state.py clear-owed` reaches the same ledger as `state.*` from
outside a tick, and `scripts/deploy_tools/deploy_playbook.py` calls
`discharge_k8s_unapplied` itself after a successful `deploy.sh` (#4087). The other readers
outside the deployer — monitor-bridge, deploy-ui, renovate-agent, the SessionStart banner and
the rest of `scripts/` — parse the ledger through `gitops_ledger` or a copy of it, and never
write these classes.

"Digest provable" has one definition. `deploy_narrow.digest_provable` is the transport: it
runs `scripts/deploy_tools/digest_provable.py` and decodes its answer. `_digest_provable`
here is the policy that reads a failure of that transport as "no role is", which keeps the
line.

Reach `deploy_alerts` qualified, never by from-import.
"""

import time

import deploy_alerts
from deploy_config import Config, log
from deploy_k8s import k8s_roles_listed
from deploy_state import OWED_K8S_DEFERRED, OWED_K8S_UNAPPLIED, DeployerState
from deploy_toolbox import DeployTools
from gitops_markers import k8s_deferred_deploy_cmd, owed_clear_cmd


def reconcile(tools: DeployTools, state: DeployerState, config: Config) -> None:
    """Discharge what a deploy since the last tick covered, then name what is still owed.

    `main()` calls this on every tick, ahead of any branch that can return. The tick that
    recorded a line fast-forwarded, so no later tick's range carries it, and a journal line
    written only where the line is written would appear once. The discharge runs FIRST, so
    the journal never names a change somebody's own `deploy.sh` has since applied: that
    deploy is invisible to every other part of the tick.
    """
    log_k8s_deferred(state)
    discharge_k8s_unapplied(tools, state, config)
    drop_deleted_k8s_unapplied(tools, state, config)
    log_k8s_unapplied(state)


def defer_bumps(state: DeployerState, origin: str, bumps) -> None:
    """Record promoted image bumps a broad tick merged and will not deploy this tick (#2449).

    Each line KEEPS ITS ORIGIN if the service is already listed, and the next deploy of the
    service clears it (`clear_applied_k8s_deferred`). The operator's clear is
    `gitops_state.py clear-owed k8s_deferred`.
    """
    state.record_owed(OWED_K8S_DEFERRED, origin, bumps, time.time())


def pending_k8s_deferred(state: DeployerState) -> set[str]:
    """The services the `k8s_deferred` marker still holds."""
    return {entry.service for entry in state.owed_pending(OWED_K8S_DEFERRED)}


def clear_applied_k8s_deferred(state: DeployerState, services) -> None:
    """Drop the `k8s_deferred` lines an apply of `services` covers, and say so.

    The deployer's own reverse of `defer_bumps`, called wherever a tick applies a k8s
    service. The operator's reverse is `gitops_state.py clear-owed k8s_deferred`,
    which is what a hand deploy needs: the deployer cannot see a `deploy.sh` somebody else ran,
    the same gap `clear-owed manual_plane` fills for a pending setup role.
    """
    cleared = state.clear_owed(OWED_K8S_DEFERRED, services)
    if cleared:
        log(f"k8s_deferred cleared for {', '.join(cleared)}: this tick applied them")


def log_k8s_deferred(state: DeployerState) -> None:
    """Name every bump the `k8s_deferred` marker still holds, in the journal.

    Called from `reconcile` on every tick, beside `deploy_defer.log_pending` and for the
    reason that call gives: the tick that recorded the bump fast-forwarded, so no later tick's range carries it and a line
    written only where the marker is written would appear once.
    """
    services = sorted(pending_k8s_deferred(state))
    if not services:
        return
    log(
        f"k8s_deferred pending: {', '.join(services)} — merged, not applied. Deploy: "
        f"{k8s_deferred_deploy_cmd(services)}, "
        f"then {owed_clear_cmd(OWED_K8S_DEFERRED, '<service>')}"
    )


def alert_and_record_deferred(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    origin: str,
    deployed: set[str],
    cs,
    declared_k8s: set[str] | None = None,
) -> None:
    """`deploy_alerts.alert_deferred`, plus the durable record of what it paged about (#2570).

    The k8s defer-and-alert post fires once per SHA and the ff-merge clears `behind_since`, so
    a hand-edited or denylisted role sits merged-and-unapplied with nothing naming it — and
    `Release Staleness Drift`, its other signal, is DOWN for any stale record anywhere in the
    fleet, so a NEW deferral adds nothing a reader can see there. `k8s_unapplied` names it, on
    the SessionStart banner and in the journal, and nothing pages on it.

    Every caller of this is an exit that leaves the range MERGED — the contention arm resets
    the tree and returns before reaching any of them — so `unrecord` owns no reverse for the
    line this writes. The wrapper lives here rather than inside `alert_deferred` because it
    writes the ledger where `alert_deferred` only pages, and this module owns every write to
    the two k8s classes.

    A SERVICE ALREADY IN `k8s_deferred` IS SKIPPED. `deploy_broad_k8s` folds a budget-deferred
    bump back into `cs.k8s` after recording it there, so without this the
    same service would hold a line in both markers and the SessionStart banner would name it
    twice — once as a deferred bump, once as an unapplied role. The paging marker wins: it
    already carries the service, and its line is the one with a threshold behind it.

    EACH LINE NAMES THE COMMIT THAT CHANGED ITS SERVICE (#3111), from `cs.k8s_origins`, and
    `origin` only where that map has no answer: the discharge asks whether a release record
    descends from the line, and a landing's record names its own commit, not the tick's tip.

    A LINE THE SERVICE'S OWN RECORD ALREADY CARRIES IS DROPPED AT ONCE (#4087). A fast-path
    landing deploys at its merge commit and only then kicks the tick that merges the range, so
    this tick records a change that is already live. `reconcile` ran before the record and
    cannot see it, and the next tick is ten minutes away. Only the own-record check runs here:
    the shared-role and render-digest proofs each start a subprocess, and the next tick's
    `reconcile` still applies them.
    """
    now = time.time()
    by_commit: dict[str, set[str]] = {}
    for service in cs.k8s - pending_k8s_deferred(state):
        by_commit.setdefault(cs.k8s_origins.get(service, origin), set()).add(service)
    for commit, services in by_commit.items():
        state.record_owed(OWED_K8S_UNAPPLIED, commit, services, now)
    recorded = set().union(*by_commit.values())
    carried = sorted(
        e.service
        for e in state.owed_pending(OWED_K8S_UNAPPLIED)
        if e.service in recorded
        and _record_carries(tools, config, tools.release_commit(e.service), e.origin)
    )
    if carried:
        state.clear_owed(OWED_K8S_UNAPPLIED, carried)
        log(
            f"k8s_unapplied not kept for {', '.join(carried)}: its release record already "
            f"carries the change"
        )
    deploy_alerts.alert_deferred(
        tools, state, config, origin, deployed, cs, declared_k8s
    )


def discharge_k8s_unapplied(
    tools: DeployTools, state: DeployerState, config: Config
) -> list[str]:
    """Drop each `k8s_unapplied` line a real deploy has since covered. Returns those dropped.

    THE RECORD DISCHARGES ITSELF, and that is what makes this marker affordable. A denylisted
    role is by definition one this deployer never applies, so a marker cleared only by a tick
    or by a hand would grow one permanent line per routine landing — forty of the fifty-four
    k8s roles are denylisted, and a banner printing forty lines is the always-red tile #2570
    objects to, moved to another surface.

    The question asked per line is "has ANY deploy of this service happened at or after the
    SHA we recorded", answered from the service's release record
    (`roles/k8s/manifests/tasks/release_stamp.yml`) and one `git merge-base --is-ancestor`.
    That is deliberately NOT the question `probe.py releases --stale-only` asks: this one
    needs no role paths and no deploy-plane narrowing, so the two cannot drift into
    disagreeing. It discharges an operator's own `deploy.sh`, which the
    deployer has no other way to see — the gap `clear-owed manual_plane` fills by hand one plane
    over.

    A record that is absent, unreadable or names a commit this checkout cannot resolve KEEPS
    the line. `is_ancestor` already reads a git error as False, and that is the direction this
    wants: a kept line is a banner entry an operator can clear in one command, where a dropped
    one loses the only record that the change was never applied.

    A SHARED ROLE HAS NO RECORD OF ITS OWN (#2643). `manifests`, `image-builder` and the rest
    carry no `containers_list` entry, so the question moves to every tag whose deploy runs
    them, as `shared_role_callers.py:caller_tags` derives them: the line drops when each of
    those carries the change. The derivation is asked once per tick, and only when a pending
    line has no record. If it fails, or names no tag, the line is kept.

    A caller carries the change when its release record descends from the line's commit —
    the fast path, which drops a line minutes after a deploy. For a role in
    `DIGEST_PROVABLE_ROLES` a caller also carries it when a render at a commit descending
    from the line's matches its applied digests (`deploy_release.render_proof`, #3057): that
    caller's bytes are what the change renders, so there is nothing left to apply there.

    A SERVICE'S OWN LINE TAKES THE SAME RENDER PROOF (#3110) when its role acts only through
    the bytes the digest covers, as `scripts/deploy_tools/digest_provable.py` derives it. The
    derivation runs only for a line its record did not already drop, and a failure reads as
    "no role is", which keeps the line.
    """
    pending = state.owed_pending(OWED_K8S_UNAPPLIED)
    records = {e.service: tools.release_commit(e.service) for e in pending}
    callers = _shared_callers(tools, config, {s for s, c in records.items() if not c})

    def descends(commit: str | None, origin: str) -> bool:
        return _record_carries(tools, config, commit, origin)

    def carries(service: str, origin: str, by_digest: bool) -> bool:
        if service not in records:
            records[service] = tools.release_commit(service)
        if descends(records[service], origin):
            return True
        return by_digest and descends(tools.render_proof(service), origin)

    provable = _digest_provable(
        tools,
        config,
        {
            e.service
            for e in pending
            if records[e.service] and not descends(records[e.service], e.origin)
        },
    )
    discharged = []
    for entry in pending:
        own = bool(records[entry.service])
        tags = {entry.service} if own else callers.get(entry.service)
        by_digest = entry.service in (provable if own else DIGEST_PROVABLE_ROLES)
        if tags and all(carries(t, entry.origin, by_digest) for t in tags):
            discharged.append(entry.service)
    if discharged:
        state.clear_owed(OWED_K8S_UNAPPLIED, discharged)
        log(
            f"k8s_unapplied discharged for {', '.join(sorted(discharged))}: a release "
            f"record or a matching render names a commit that carries the change"
        )
    return sorted(discharged)


# DECIDED: only `manifests` discharges on a render digest among SHARED roles (#3057); a
# service's own role is derived, not listed (#3110, `scripts/deploy_tools/digest_provable.py`).
# A digest match proves the bytes `manifests_digest` and `secret_digest` cover, and nothing a
# role does outside them. `manifests` renders those bytes, so most of its changes either move
# a caller's digest or take effect on the next deploy of any caller without being "behind".
# The gap taken is a change to HOW it applies — the prune, the Secret-key reconcile, an apply
# flag — which can leave live state different from what that change would produce while no
# digest moves: the line discharges and the difference waits for the next deploy. `probe.py
# releases` has taken the same gap since #3046. A line that never discharged until a full
# deploy was the defect (#2643), so the gap is accepted. Every other shared role acts outside
# the digest: `image-builder`'s `build-job.yaml.j2` is outside it, `arr-notification`
# writes an app's database over its API, and `cronjob-gate`, `longhorn-api`,
# `volume-snapshot` and `volume-revert` render nothing at all, so they keep the record-only
# rule.
DIGEST_PROVABLE_ROLES = frozenset({"manifests"})


def _record_carries(
    tools: DeployTools, config: Config, commit: str | None, origin: str
) -> bool:
    """Whether a release record naming `commit` carries the change merged at `origin`.

    No commit is no evidence, and `is_ancestor` reads a git error as False, so every unknown
    keeps the line.
    """
    return bool(commit) and tools.is_ancestor(config.repo, origin, commit)


def _shared_callers(
    tools: DeployTools, config: Config, services: set[str]
) -> dict[str, set[str]]:
    """`tools.shared_role_callers` for `services`, or no callers at all when it fails.

    Any exception, not a tuple: the call decodes a subprocess's JSON, and the one safe
    reading of a failure is "no evidence", which keeps every line it would have judged.
    """
    if not services:
        return {}
    try:
        return tools.shared_role_callers(config.repo, services)
    except Exception as exc:
        log(
            f"k8s_unapplied: could not derive the callers of {', '.join(sorted(services))} "
            f"({type(exc).__name__}: {exc}) — keeping their lines"
        )
        return {}


def _digest_provable(
    tools: DeployTools, config: Config, services: set[str]
) -> set[str]:
    """`tools.digest_provable` for `services`, or none of them when it fails.

    None is the safe reading for the reason `_shared_callers` gives: it keeps every line.
    """
    if not services:
        return set()
    try:
        return tools.digest_provable(config.repo, services)
    except Exception as exc:
        log(
            f"k8s_unapplied: could not derive which of {', '.join(sorted(services))} a "
            f"render digest proves ({type(exc).__name__}: {exc}) — keeping their lines"
        )
        return set()


def log_k8s_unapplied(state: DeployerState) -> None:
    """Name every k8s role change the `k8s_unapplied` marker still holds, in the journal.

    Called from `reconcile` beside `log_k8s_deferred`, and for the reason that call gives: the
    tick that recorded the change fast-forwarded, so no later tick's range carries it and a
    line written only where the marker is written would appear once. Nothing pages on this,
    so the journal and the SessionStart banner are the whole of its reach.
    """
    services = sorted({e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)})
    if not services:
        return
    log(
        f"k8s_unapplied pending: {', '.join(services)} — merged, not applied, and this "
        f"deployer never applies them. Deploy: {k8s_deferred_deploy_cmd(services)}"
    )


def k8s_roles_deleted_at(
    tools: DeployTools, config: Config, ref: str, roles: set[str]
) -> set[str]:
    """The roles in `roles` whose directory is gone from `ref`'s tree (#3568, #3569).

    A deleted role owes nothing: no play can run it, and an `include_role` still naming it
    fails with "role not found" rather than applying anything. A `k8s_unapplied` line for one
    never discharges, because a role with no callers has no tag whose deploy could carry it
    (`volume-claim`, 2026-10-05). `deploy_phases.plan_tick` asks this at origin so the tick writes
    no such line; `drop_deleted_k8s_unapplied` asks it at `HEAD` to drop a line an earlier tick
    wrote before the range that deleted the role. `land_shared.shared_roles` drops the
    same roles on the landing side.

    An unreadable or empty listing drops nothing, because a kept line costs one `clear-owed`
    and a dropped one loses the only record of a change.
    """
    if not roles:
        return set()
    try:
        listing = tools.run(
            ["git", "ls-tree", "--name-only", ref, "ansible/roles/k8s/"],
            cwd=config.repo,
        )
    except Exception as exc:
        log(f"could not list the k8s roles at {ref[:8]} ({exc}) — dropping none")
        return set()
    present = k8s_roles_listed(listing)
    if not present:
        return set()
    return roles - present


def drop_deleted_k8s_unapplied(
    tools: DeployTools, state: DeployerState, config: Config
) -> set[str]:
    """Drop each `k8s_unapplied` line whose role directory is gone from `HEAD` (#3569).

    The line was written before a later range deleted the role, which `plan_tick` no longer
    records, and `discharge_k8s_unapplied` can never drop: no record or caller is left to
    carry it. `reconcile` runs this after that discharge, on the merged checkout.
    """
    pending = {e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)}
    deleted = k8s_roles_deleted_at(tools, config, "HEAD", pending)
    if deleted:
        state.clear_owed(OWED_K8S_UNAPPLIED, sorted(deleted))
        log(
            f"k8s_unapplied dropped for {', '.join(sorted(deleted))}: role deleted at HEAD"
        )
    return deleted
