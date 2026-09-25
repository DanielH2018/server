#!/usr/bin/env python3
"""What a broad tick does with the half of a range it will not apply: park it, or record it.

Two shapes, and the difference between them is whether an operator can be told what to run.

**Park** (`park`). A bring-up playbook — `_BROAD_MANUAL_PREFIXES` — runs by hand by
construction, and a setup path naming no role at all (a file directly under `roles/setup/`)
has no command to print and no role to record. Both keep the old behaviour exactly: no
ff-merge, a journal line every tick, a page once per SHA. Staying parked is what keeps
`behind_since` set, which is the only durable signal those two have.

**Record** (`record`). A setup ROLE `initial_setup.yml` does not include — `k3s`, applied by
`k3s-bringup.yml`, and `common`, applied by no playbook at all. The tick fast-forwards and
writes the role to the `manual_plane` marker instead of parking.

# DECIDED: an unapplyable setup ROLE no longer parks the tick. Parking was the signal only
because nothing else was, and everybody else paid for it. Over the seven days to 2026-09-11
ten park episodes spanned 30 ticks — the longest about forty minutes — and while parked every
other session's landing exits 4 from `deploy.sh` (tree behind origin) until a hand pulls the
primary checkout. None of that waiting brought the hand-apply any closer: the role needs
`ansible-playbook ansible/k3s-bringup.yml --tags k3s` whether or not the range is merged. The
marker is the signal now, and it is a better one than the park was: it names the role and the
command, it outlives the convergence the ff-merge produces (where `behind_since` does not),
monitor-bridge pages on its age, `land.sh` prints the same clear command, and both the
deployer and an operator can clear it. `cs.broad_manual` still parks, because a half-applied
bring-up playbook is a state no marker makes safe.

Reach `deploy_alerts` qualified, never by from-import.
"""

import time
from typing import NamedTuple

import deploy_alerts
import deploy_narrow
from deploy_changes import setup_role_playbook, setup_role_tag
from deploy_config import Config, log
from deploy_remediation import (
    broad_park_reason,
    broad_remediation,
    manual_plane_remediation,
)
from deploy_state import NO_PLAYBOOK, DeployerState
from gitops_markers import k8s_deferred_clear_cmd, k8s_deferred_deploy_cmd
from deploy_tick_types import TickTarget
from deploy_toolbox import DeployTools

INITIAL_SETUP = "ansible/initial_setup.yml"


def for_contention(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    exc: BaseException,
) -> int:
    """A service lock stayed busy: undo the range and let the next tick re-evaluate.

    The third deferral shape, and the only one that is not about a playbook nobody here can
    run. Contention is not a failed deploy: nothing was applied, so holding the SHA would park
    every later tick behind a lock that has since been released, and rolling back would
    redeploy a version that is already live. The ff-merge IS undone, because `local..origin`
    carrying the range is what makes the next tick look at it again.

    The streak is recorded in `contention_since`, because the reset leaves no other durable
    trace: `last_run` advances (the tick completed), `hold_sha` stays empty (nothing failed),
    and `behind_since` ages toward a six-hour page sized for a dirty tree. A wedged operator
    `deploy.sh` holding one service lock therefore deferred every tick silently for as long
    as it lived (issue #1847). monitor-bridge pages once the streak's first-seen stamp is
    older than `GITOPS_CONTENTION_MAX_MIN`, and the SessionStart banner names the lock.

    Args:
        tools: the tick's boundaries; its `run` performs the reset.
        state: the marker files, for the streak.
        config: the tick's config, for the repo path.
        target: the tick's refs; `local` is where the reset lands.
        exc: the `deploy_locks.ServiceLockBusy` raised, naming the tag and the seconds.

    Returns:
        0. The tick completed, so `last_run` is written and the deployer reads alive.
    """
    entry = state.record_contention(
        target.origin, getattr(exc, "lock", ""), time.time()
    )
    log(
        f"{exc} — deferring to the next tick (consecutive deferral {entry.count}, "
        f"first at {int(entry.first_seen)})"
    )
    tools.run(["git", "reset", "--hard", target.local], cwd=config.repo)
    return 0


def unapplyable_setup_roles(cs) -> list[str]:
    """The setup roles in this range that NO playbook this deployer runs can apply.

    Sorted, so the journal line and the marker agree on an order.
    """
    return sorted(
        role for role in cs.setup_roles if setup_role_playbook(role) != INITIAL_SETUP
    )


def parks_the_tick(cs, setup_tags: set[str], pending: list[str]) -> bool:
    """Whether this range must be deferred WITHOUT a fast-forward.

    Args:
        cs: the range's `ChangeSet`.
        setup_tags: `setup_tags_for(paths)` — the setup tags that did resolve.
        pending: `unapplyable_setup_roles(cs)`.

    A setup-plane range with neither a resolvable tag nor a nameable role is the second half
    of this, and it is not hypothetical padding: `_note_setup_role` matches
    `roles/setup/<name>/`, so a file sitting directly under `roles/setup/` is broad,
    unroutable and nameless at once. Fast-forwarding it would apply nothing, record nothing
    and say nothing.
    """
    return cs.broad_manual or (cs.broad_setup and not setup_tags and not pending)


def park(
    tools: DeployTools, state: DeployerState, config: Config, origin: str, cs
) -> None:
    """Log the park on every tick, and page once per SHA. Nothing is merged."""
    remediation = broad_remediation(
        cs.broad_deploy, cs.broad_setup, cs.setup_roles, config.branch
    )
    # Say so in the JOURNAL, every tick. `alert_once` below throttles the Discord page to one
    # per SHA, and until 2026-09-09 that throttle also decided what the journal said: from the
    # second tick behind a range this arm logged NOTHING. daniel-box then sat nine commits
    # behind origin for twenty minutes with the only per-tick line coming from an unrelated
    # comment-only path, which read as the cause and was not (#1467). An operator reads this
    # journal when `land.sh` exits 4, and a page they already received an hour ago is not there.
    log(
        f"origin {origin[:8]}: parked, nothing merged — {broad_park_reason(cs)}. "
        f"Apply by hand: {remediation}"
    )
    # A park doesn't ff-merge, so it re-evals next tick — the per-SHA marker (inside
    # alert_once) stops a re-queue while the pending queue owns redelivery. Name the RIGHT
    # playbook per plane: deploy.yml applies only container roles, so a setup-plane change
    # needs initial_setup.yml (2026-07-16 review M1).
    deploy_alerts.alert_once(
        tools,
        state,
        config,
        "broad_alerted",
        "broad",
        origin,
        deploy_alerts.broad_deferred_alert(origin, remediation),
    )


def narrow_tags_for(
    tools: DeployTools, config: Config, target: TickTarget, role: str
) -> frozenset[str] | None:
    """The narrowest tags `role`'s own change needs, or None when nothing could narrow it.

    The role tag applies far more than any one change to it needs: `--tags k3s` reapplies
    MetalLB, Longhorn, the crons, CoreDNS and the node config and arms three gated
    control-plane tasks, where a `templates/readonly-rbac.yaml.j2` edit needs `--tags
    kubeconfig` (#2294, #2307). `scripts/deploy_tools/narrow_setup.py` maps the changed paths
    to the tags of the tasks that read them.

    ANY failure is None, which every reader renders as today's role tag. A derivation that
    cannot answer must widen: a `--tags` value matching nothing makes Ansible exit 0 having
    applied nothing, and an operator reading a green recap over an unapplied change is worse
    off than one reading a command that applies too much. `except Exception` is the right
    width for the same reason `deploy_narrow._deploy_plane` uses it — the call decodes a
    subprocess's output, so it raises more than `SubprocessError`, and an escape here would
    park a tick that has already fast-forwarded.

    A role no playbook applies is None without asking: its remediation names no `--tags`
    at all, so there is nothing a narrowing could replace.
    """
    playbook = setup_role_playbook(role)
    if playbook is None:
        return None
    try:
        rc, out = tools.narrow_setup_role(
            config.repo,
            role,
            setup_role_tag(role),
            playbook,
            target.local,
            target.origin,
            deploy_narrow.NARROW_SETUP_TIMEOUT_S,
        )
    except Exception as exc:
        log(f"narrow-setup: {role} not narrowed ({type(exc).__name__}: {exc})")
        return None
    if rc != 0:
        return None
    return frozenset(tag for tag in out.split(",") if tag) or None


class Recorded(NamedTuple):
    """What one broad tick wrote, and everything `unrecord` needs to take it back.

    Attributes:
        roles: the roles whose `manual_plane` LINE this tick appended. A role a previous tick
            recorded keeps its first-seen stamp and is not in here.
        tags_before: role tag -> its `manual_plane_tags` row as it stood BEFORE this tick, or
            None where the role had no row. One entry per role the tick wrote a row for,
            which is every role it was handed — including the ones already pending.
        broad_applied_before: the `broad_applied` marker as it stood before the tick's plan
            loop, or None where there was none. Snapshotted whether or not the tick records a
            role, because the loop writes that marker on a range with nothing pending too.
        k8s_deferred: the services whose `k8s_deferred` LINE this tick appended for a staging
            demotion. A service a previous tick recorded keeps its first-seen stamp and is not
            in here, exactly as `roles` leaves an already-pending role out.
    """

    roles: list[str]
    tags_before: dict[str, frozenset[str] | None]
    broad_applied_before: str | None
    k8s_deferred: list[str]


def nothing_recorded(state: DeployerState) -> Recorded:
    """What `handle_broad` passes to `unrecord` for a tick that records no role.

    A module constant cannot serve, and that is #2382 in one line: `broad_applied_before`
    varies per tick, and the range the bug is reported on — an applyable setup plane, then a
    busy lock in a later plan — is exactly the one with nothing pending. A shared empty
    snapshot there would leave the stale marker standing.
    """
    return Recorded([], {}, state.broad_applied, [])


def record(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    roles: list[str],
) -> Recorded:
    """Record each role this tick merged past and cannot apply, then page once per SHA.

    A role already in the marker keeps its first-seen stamp, which is the age monitor-bridge
    pages on. The page goes out on the `broad` channel — the same one `park` uses, which a
    range can never take as well as this one.

    The narrow tags go in a sidecar marker for EVERY role in `roles`, not only the ones this
    tick added: a second range touching an already-pending role adds work the first line
    cannot describe, and `record_manual_plane_tags` unions the two.

    The journal line here names only what this tick ADDED. `main()` already logs the whole
    pending set on every tick, including this one, so logging the set again here printed it
    twice whenever a role was already listed.

    Returns:
        A `Recorded` carrying each half of what this tick did, because they need different
        reverses (#2320). The lines it appended are dropped outright. The rows it WIDENED
        belong to a role an earlier range made pending, so they are put back to the snapshot
        taken here — the union is not invertible, and dropping the line with them would take
        back a range that is still merged. The `broad_applied` marker is snapshotted here
        because this runs before the plan loop that writes it (#2382).
    """
    now = time.time()
    origin = target.origin
    broad_applied_before = state.broad_applied
    before = state.manual_plane_tags_pending()
    tags_before = {
        setup_role_tag(role): before.get(setup_role_tag(role)) for role in roles
    }
    recorded = [
        role
        for role in roles
        if state.record_manual_plane(
            origin, setup_role_playbook(role) or NO_PLAYBOOK, setup_role_tag(role), now
        )
    ]
    for role in roles:
        state.record_manual_plane_tags(
            setup_role_tag(role),
            narrow_tags_for(tools, config, target, role),
            line_predates=role not in recorded,
        )
    narrow = state.manual_plane_tags_pending()
    if recorded:
        log(
            f"manual_plane recorded: {', '.join(recorded)} — merged, not applied; "
            + manual_plane_remediation(set(recorded), narrow)
        )
    deploy_alerts.alert_once(
        tools,
        state,
        config,
        "broad_alerted",
        "broad",
        origin,
        deploy_alerts.manual_plane_alert(
            origin,
            manual_plane_remediation(set(roles), narrow),
            state.path("manual_plane"),
        ),
    )
    return Recorded(recorded, tags_before, broad_applied_before, [])


def record_demoted(
    state: DeployerState, origin: str, recorded: Recorded, demoted: set[str]
) -> Recorded:
    """Record the bumps the staging gate demoted, and fold them into `recorded`.

    Returns:
        `recorded` carrying the lines this tick appended, so `unrecord` can take exactly
        those back.

    Args:
        state: the marker files.
        origin: the SHA this tick merged. The bump is live in the tree at that commit.
        recorded: what `record` or `nothing_recorded` returned for this tick.
        demoted: the bumps `gate_broad_k8s` folded back into the defer-and-alert channel.
    """
    # DECIDED: of the three classes on the defer-and-alert channel, the STAGING-DEMOTED bump
    # gets the `k8s_deferred` marker and the other two do not (#2471). #2449 scoped the marker
    # to the budget deferral, and a demotion has the same two properties that case has: the
    # tick chose it rather than a person, and nothing reports it a second time, because the
    # range is merged and no later `local..origin` carries the bump. A hand-edited k8s role is
    # merged by the person landing it, whose `land.sh` is watching; a denylisted role's
    # ordinary change is routine, and forty of the fifty-four k8s roles are denylisted, so
    # recording those would hold GitOps Deploy — Status red as normal operation — the failure
    # the age gate on `manual_plane` was sized to avoid. The denylisted class keeps `Release
    # Staleness Drift` as its durable signal; #2570 carries whether that is enough.
    #
    # Called at the ff-merge, for the reason `record` is: that is the moment the bump becomes
    # merged-and-unapplied, and a broad apply that FAILS returns from its except arm, where a
    # record placed after it never runs. `gate_broad_k8s` decided the demotion but cannot
    # write it — it runs BEFORE the merge, and a contention arm below resets the tree.
    added = state.record_k8s_deferred(origin, demoted, time.time())
    if added:
        log(
            f"k8s_deferred recorded: {', '.join(added)} — staging rejected the bump, and the "
            f"range merged without it"
        )
    return recorded._replace(k8s_deferred=added)


def unrecord(state: DeployerState, origin: str, recorded: Recorded) -> None:
    """Take back everything `record` wrote, for a tick whose ff-merge was undone.

    A pending role means merged-and-unapplied. When the tick resets to `local` — which
    `for_contention` does, because nothing was applied — the merge half stops being true, so
    the marker would page for six hours about work no tree carries. The dedupe page is cleared
    with it, but only when it names THIS origin: a page for an earlier SHA is somebody else's.

    THE `broad_applied` MARKER GOES BACK TOO (#2382). A plan that applied before the reset
    wrote it, and after the reset it names a SHA no tree here carries — which `land.sh` reads
    as "this tick applied that plane". The plane really was applied, and the next tick
    re-crosses the whole range and re-applies it idempotently, so the marker the reset leaves
    behind is a claim about a range that has to be made again.

    THE TWO MANUAL-PLANE HALVES NEED DIFFERENT REVERSES (#2320). A role whose line this tick
    appended is cleared outright, and `clear_manual_plane` takes its row with it. A role ALREADY
    pending keeps its line — an earlier range is still merged — and only the row this tick
    widened is put back. `record` returned the earlier value because a subtraction cannot do
    it: this tick's derivation may have collapsed the row to the empty set, and nothing
    subtracted from an empty set recovers the earlier range's tags.

    Args:
        state: the marker files.
        origin: the SHA this tick recorded under.
        recorded: what `record` returned for this tick.
    """
    state.restore_broad_applied(recorded.broad_applied_before)
    cleared = {setup_role_tag(role) for role in recorded.roles}
    for tag in cleared:
        state.clear_manual_plane(tag)
    for tag, before in recorded.tags_before.items():
        if tag not in cleared:
            state.restore_manual_plane_tags(tag, before)
    # Only when this tick appended a line. A tick that only widened an already-pending role's
    # row leaves the role pending either way, so clearing the dedupe there re-paged the same
    # SHA on every contended tick.
    if recorded.roles and state.read("broad_alerted") == origin:
        state.write("broad_alerted", None)
    # Only the `k8s_deferred` lines this tick appended: a service an earlier range left
    # pending is still merged-and-unapplied after the reset, and dropping its line would
    # lose the only durable record of it.
    state.clear_k8s_deferred(recorded.k8s_deferred)


def pending_k8s_deferred(state: DeployerState) -> set[str]:
    """The services the `k8s_deferred` marker still holds."""
    return {entry.service for entry in state.k8s_deferred_pending()}


def clear_applied_k8s_deferred(state: DeployerState, services) -> None:
    """Drop the `k8s_deferred` lines an apply of `services` covers, and say so.

    The deployer's own reverse of `DeployerState.record_k8s_deferred`, called wherever a tick
    applies a k8s service. The operator's reverse is `gitops_state.py clear-k8s-deferred`,
    which is what a hand deploy needs: the deployer cannot see a `deploy.sh` somebody else ran,
    the same gap `clear-manual-plane` fills for a pending setup role.
    """
    cleared = state.clear_k8s_deferred(services)
    if cleared:
        log(f"k8s_deferred cleared for {', '.join(cleared)}: this tick applied them")


def log_k8s_deferred(state: DeployerState) -> None:
    """Name every bump the `k8s_deferred` marker still holds, in the journal.

    Called from `main()` beside `log_pending`, and for the reason that call gives: the tick
    that recorded the bump fast-forwarded, so no later tick's range carries it and a line
    written only where the marker is written would appear once.
    """
    services = sorted(pending_k8s_deferred(state))
    if not services:
        return
    log(
        f"k8s_deferred pending: {', '.join(services)} — merged, not applied. Deploy: "
        f"{k8s_deferred_deploy_cmd(services)}, then {k8s_deferred_clear_cmd()}"
    )


def clear_applied(state: DeployerState, playbook: str, tags: list[str]) -> None:
    """Drop the pending roles this apply covered, and say so.

    The deployer's own reverse of `record`. No role reaches it today — every role the marker
    can hold is applied by a playbook this tick never runs — and it is what a role promoted
    into `initial_setup.yml` needs on the day it is.
    """
    for role in state.clear_manual_plane_applied(playbook, tags):
        log(
            f"manual_plane cleared for {role}: this tick applied {playbook} --tags {role}"
        )


def log_pending(state: DeployerState) -> None:
    """Name every role the `manual_plane` marker still holds, in the journal.

    Called from `main()` and from nowhere else, before any branch can return. The tick that
    writes the marker fast-forwards, so from the next tick on the deployer is converged and
    re-enters the broad arm never again — a line written only where the marker is written
    would appear once, and an operator reading the journal an hour later would see an idle
    deployer with an unapplied role. That invisibility is what #1467 cost, one plane over.
    The writing tick says its own piece through `record`, which names what it ADDED: this
    call runs ahead of it, against a marker that does not hold the new role yet.
    """
    roles = sorted({e.role for e in state.manual_plane_pending()})
    if not roles:
        return
    log(
        f"manual_plane pending: {', '.join(roles)} — apply by hand: "
        + manual_plane_remediation(set(roles), state.manual_plane_tags_pending())
    )
