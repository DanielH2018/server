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

import deploy_alerts
from deploy_changes import setup_role_playbook, setup_role_tag
from deploy_config import Config, log
from deploy_remediation import (
    broad_park_reason,
    broad_remediation,
    manual_plane_remediation,
)
from deploy_state import NO_PLAYBOOK, DeployerState
from deploy_tick_types import TickTarget
from deploy_toolbox import DeployTools

INITIAL_SETUP = "ansible/initial_setup.yml"


def for_contention(
    tools: DeployTools, config: Config, target: TickTarget, exc: BaseException
) -> int:
    """A service lock stayed busy: undo the range and let the next tick re-evaluate.

    The third deferral shape, and the only one that is not about a playbook nobody here can
    run. Contention is not a failed deploy: nothing was applied, so holding the SHA would park
    every later tick behind a lock that has since been released, and rolling back would
    redeploy a version that is already live. The ff-merge IS undone, because `local..origin`
    carrying the range is what makes the next tick look at it again.

    Args:
        tools: the tick's boundaries; its `run` performs the reset.
        config: the tick's config, for the repo path.
        target: the tick's refs; `local` is where the reset lands.
        exc: the `deploy_locks.ServiceLockBusy` raised, naming the tag and the seconds.

    Returns:
        0. The tick completed, so `last_run` is written and the deployer reads alive.
    """
    log(f"{exc} — deferring to the next tick")
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


def record(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    origin: str,
    roles: list[str],
) -> list[str]:
    """Record each role this tick merged past and cannot apply, then page once per SHA.

    A role already in the marker keeps its first-seen stamp, which is the age monitor-bridge
    pages on. The page goes out on the `broad` channel — the same one `park` uses, which a
    range can never take as well as this one.

    The journal line here names only what this tick ADDED. `main()` already logs the whole
    pending set on every tick, including this one, so logging the set again here printed it
    twice whenever a role was already listed.

    Returns:
        The roles this tick added, which is what `unrecord` takes back when the ff-merge that
        made them pending is rolled back. A role a previous tick recorded is not in it.
    """
    now = time.time()
    recorded = [
        role
        for role in roles
        if state.record_manual_plane(
            origin, setup_role_playbook(role) or NO_PLAYBOOK, setup_role_tag(role), now
        )
    ]
    if recorded:
        log(
            f"manual_plane recorded: {', '.join(recorded)} — merged, not applied; "
            + manual_plane_remediation(set(recorded))
        )
    deploy_alerts.alert_once(
        tools,
        state,
        config,
        "broad_alerted",
        "broad",
        origin,
        deploy_alerts.manual_plane_alert(
            origin, manual_plane_remediation(set(roles)), state.path("manual_plane")
        ),
    )
    return recorded


def unrecord(state: DeployerState, origin: str, roles: list[str]) -> None:
    """Take back the lines `record` wrote, for a tick whose ff-merge was undone.

    A pending role means merged-and-unapplied. When the tick resets to `local` — which
    `for_contention` does, because nothing was applied — the merge half stops being true, so
    the marker would page for six hours about work no tree carries. The dedupe page is cleared
    with them, but only when it names THIS origin: a page for an earlier SHA is somebody else's.

    Args:
        state: the marker files.
        origin: the SHA this tick recorded under.
        roles: what `record` returned, so a role an earlier tick recorded is left alone.
    """
    for role in roles:
        state.clear_manual_plane(setup_role_tag(role))
    if roles and state.read("broad_alerted") == origin:
        state.write("broad_alerted", None)


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
        + manual_plane_remediation(set(roles))
    )
