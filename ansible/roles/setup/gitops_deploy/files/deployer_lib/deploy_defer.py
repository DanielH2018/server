#!/usr/bin/env python3
"""What a broad tick does with the half of a range it will not apply: park it, or record it.

Two shapes, and the difference between them is whether an operator can be told what to run.

**Park** (`park`). A bring-up playbook — `_BROAD_MANUAL_PREFIXES` — runs by hand by
construction, and a setup path naming no role at all (a file directly under `roles/setup/`)
has no command to print and no role to record. Both keep the old behaviour exactly: no
ff-merge, a journal line every tick, a page once per SHA. Staying parked is what keeps
`behind_since` set, which is the only durable signal those two have.

A third shape parks too, and it is the only one a setup ROLE reaches: the routing subprocess
failed or timed out (`routing_failed`), so the tick cannot tell an
applyable role from one it must record. The next tick derives again (#4326).

**Record** (`record`). A setup ROLE `initial_setup.yml` does not include — `k3s`, applied by
`k3s-bringup.yml`, and `common`, applied by no playbook at all — or one the routing came back
without placing (`UNROUTED_PLAYBOOK`). The tick fast-forwards and writes the role to the `owed`
ledger's `manual_plane` class instead of parking. A role whose directory the range deletes is
neither: it owes nothing, and `drop_deleted_setup_roles` drops it. A line recorded BEFORE a
later range deleted the role is dropped by `drop_deleted_manual_plane` (#4334).

# DECIDED: an unapplyable setup ROLE no longer parks the tick, with one exception: a
transient routing failure (the subprocess failed or timed out) parks until a tick can route.
A deterministic answer, such as a deleted role or an unplaced one, never parks: no retry
changes it, and the commit that fixes it lands only after the park clears (#4326). Parking was
the signal only because nothing else was, and everybody else paid for it. Over the seven days to 2026-09-11
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

from deployer_lib import deploy_alert_text
from deployer_lib import deploy_alerts
from deployer_lib import deploy_narrow
from deployer_lib.deploy_changes import (
    ChangeSet,
    is_routed,
    keys_naming_no_role,
    role_of,
    routing_failed,
    services_from_changed_paths,
    setup_role_host,
    setup_roles_for,
    setup_role_playbook,
    setup_role_tag,
    tag_selects_an_off_host_role,
    tick_applies_setup_role,
)
from deployer_lib.deploy_config import WARNING, Config, log
from deployer_lib.deploy_setup_roles import SecretReadersRefused, setup_role_agent_hosts
from deployer_lib.deploy_remediation import (
    broad_park_reason,
    broad_remediation,
    manual_plane_remediation,
)
from deployer_lib.deploy_state import NO_PLAYBOOK, DeployerState
from gitops_markers import UNROUTED_PLAYBOOK
from deployer_lib.deploy_tick_types import TickTarget
from deployer_lib.deploy_toolbox import DeployTools


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
        f"contention_since: {exc} — deferring to the next tick (consecutive deferral {entry.count}, "
        f"first at {int(entry.first_seen)})"
    )
    tools.run(["git", "reset", "--hard", target.local], cwd=config.repo)
    return 0


def unapplyable_setup_roles(cs) -> list[str]:
    """The setup roles in this range that NO playbook run on this host can apply.

    A role `initial_setup.yml` gates off the tick's host is one of them (#3933). Sorted, so
    the journal line and the marker agree on an order.
    """
    return sorted(role for role in cs.setup_roles if not tick_applies_setup_role(role))


def setup_roles_listed(listing: str) -> set[str]:
    """Every setup role directory a `git ls-tree --name-only <ref> ansible/roles/setup/` names."""
    return {
        parts[3]
        for parts in (line.split("/") for line in listing.splitlines())
        if len(parts) >= 4 and parts[:3] == ["ansible", "roles", "setup"] and parts[3]
    }


def drop_deleted_setup_roles(
    tools: DeployTools, config: Config, origin: str, cs: ChangeSet, paths: list[str]
) -> ChangeSet:
    """`cs` without the setup roles whose directory is gone at `origin` (#4326).

    The setup half of `k8s_roles_deleted_at`. A deleted role owes nothing, and the routing
    never places it, so before this a range deleting one parked on every tick. The range is
    classified again without the deleted roles' paths, so a range that touched nothing else
    in the setup plane no longer reads as a setup path naming no role, which parks too.

    An unreadable or empty listing drops nothing, as for the k8s roles.
    """
    if not cs.setup_roles:
        return cs
    try:
        listing = tools.run(
            ["git", "ls-tree", "--name-only", origin, "ansible/roles/setup/"],
            cwd=config.repo,
        )
    except Exception as exc:
        log(
            f"manual_plane: could not list the setup roles at {origin[:8]} ({exc}) — dropping none",
            level=WARNING,
        )
        return cs
    present = setup_roles_listed(listing)
    deleted = cs.setup_roles - present if present else set()
    if not deleted:
        return cs
    log(
        f"manual_plane: {', '.join(sorted(deleted))}: setup role directory deleted at {origin[:8]} — "
        "nothing left to apply, so no manual_plane line"
    )

    def gone(p: str) -> bool:
        at = role_of(p)
        return at is not None and at.plane == "setup" and at.role in deleted

    kept = services_from_changed_paths([p for p in paths if not gone(p)])
    kept.setup_roles -= deleted
    return kept


def route_secret_readers(
    tools: DeployTools, config: Config, target: TickTarget, cs: ChangeSet
) -> None:
    """Route the setup roles reading a `secrets.yml` key the range moved into `cs` (#5436).

    `secrets.yml` sits under no setup role, so PR #5405's key for `claude_code` applied
    nothing. Each reader path goes through `setup_roles_for`, less the shippers of a vars file
    that read none of its secret-built keys, and `handle_broad` then applies or records each
    role as for its own edit. A refusal routes nothing and the `secrets` page names the
    consumers. Any other failure parks, as no later range carries the change again. Runs
    after `drop_deleted_setup_roles`, whose reclassification would lose the roles.
    """
    if not cs.secrets:
        return
    try:
        paths, unread = tools.secret_setup_readers(
            config.repo, target.local, target.origin
        )
    except SecretReadersRefused as exc:
        log(f"secret-readers: no setup role routed ({exc})")
        return
    except Exception as exc:
        log(f"secret-readers: {type(exc).__name__}: {exc} — parking", level=WARNING)
        cs.secret_readers_failed = cs.broad = True
        return
    roles = {r for p in paths for r in setup_roles_for(p) - set(unread.get(p, ()))}
    if not roles:
        return
    log(
        f"secret-readers: secrets.yml moved a key {', '.join(sorted(roles))} reads "
        f"({', '.join(sorted(paths))}) — routing them as setup-plane changes"
    )
    cs.secret_setup_roles |= roles
    cs.setup_roles |= roles
    cs.broad = cs.broad_setup = True


def secret_reader_tags(cs: ChangeSet) -> set[str]:
    """The `initial_setup.yml` tags of the `cs.secret_setup_roles` the tick's run applies.

    The other half of `setup_tags_for(paths)`, with its filter: a role gated off this host or
    applied by another playbook gets no tag here and is recorded in `manual_plane` instead.
    """
    return {
        setup_role_tag(role)
        for role in cs.secret_setup_roles
        if tick_applies_setup_role(role)
    }


def drop_deleted_manual_plane(
    tools: DeployTools, state: DeployerState, config: Config
) -> set[str]:
    """Drop each `manual_plane` line whose setup role is gone from `HEAD` (#4334).

    `deploy_k8s_owed.drop_deleted_k8s_unapplied` did the same for the k8s class until D6.12
    deleted it. A line written before a
    later range deleted the role never clears: no play can apply the role, so `clear_applied`
    never matches it. `main()` runs this after the routing is adopted at `HEAD`, which the
    key check reads. An unreadable listing drops nothing, as `drop_deleted_setup_roles` does.
    """
    keys = {e.role for e in state.manual_plane_pending()}
    if not keys:
        return set()
    try:
        listing = tools.run(
            ["git", "ls-tree", "--name-only", "HEAD", "ansible/roles/setup/"],
            cwd=config.repo,
        )
    except Exception as exc:
        log(
            f"manual_plane: could not list the setup roles at HEAD ({exc}) — dropping none",
            level=WARNING,
        )
        return set()
    gone = keys_naming_no_role(keys, setup_roles_listed(listing))
    for key in sorted(gone):
        state.clear_manual_plane(key)
        log(f"manual_plane: dropped for {key}: setup role deleted at HEAD")
    return gone


def parks_the_tick(cs, setup_tags: set[str], pending: list[str]) -> bool:
    """Whether this range must be deferred WITHOUT a fast-forward.

    A range carrying a setup role parks when the routing derivation failed or timed out, so
    the next tick derives again rather than fast-forwarding past a change nothing routed. A
    role the routing came back without is in `pending` instead, and is recorded (#4326).

    Args:
        cs: the range's `ChangeSet`.
        setup_tags: `setup_tags_for(paths)` — the setup tags that did resolve.
        pending: `unapplyable_setup_roles(cs)`.

    A setup-plane range with neither a resolvable tag nor a nameable role is the second half
    of this, and it is not hypothetical padding: `setup_roles_for` matches
    `roles/setup/<name>/`, so a file sitting directly under `roles/setup/` is broad,
    unroutable and nameless at once. Fast-forwarding it would apply nothing, record nothing
    and say nothing.
    """
    if cs.broad_manual or cs.secret_readers_failed:
        return True
    if cs.setup_roles and routing_failed():
        return True
    return cs.broad_setup and not setup_tags and not pending


def park(
    tools: DeployTools, state: DeployerState, config: Config, origin: str, cs
) -> None:
    """Log the park on every tick, and page once per SHA. Nothing is merged."""
    remediation = broad_remediation(
        cs.broad_deploy,
        cs.broad_setup,
        cs.setup_roles,
        config.branch,
        playbooks=cs.manual_playbooks,
        secret_readers_failed=cs.secret_readers_failed,
    )
    # Say so in the JOURNAL, every tick. `alert_once` below throttles the Discord page to one
    # per SHA, and until 2026-09-09 that throttle also decided what the journal said: from the
    # second tick behind a range this arm logged NOTHING. daniel-box then sat nine commits
    # behind origin for twenty minutes with the only per-tick line coming from an unrelated
    # comment-only path, which read as the cause and was not (#1467). An operator reads this
    # journal when `land.sh` exits 4, and a page they already received an hour ago is not there.
    log(
        f"park: origin {origin[:8]}: parked, nothing merged — {broad_park_reason(cs)}. "
        f"{'' if cs.secret_readers_failed else 'Apply by hand: '}{remediation}"
    )
    # A park doesn't ff-merge, so it re-evals next tick; the per-SHA marker (inside alert_once)
    # stops a re-queue. The remediation names the RIGHT playbook per plane (2026-07-16 M1).
    deploy_alerts.alert_once(
        tools,
        state,
        config,
        "broad",
        origin,
        deploy_alert_text.broad_deferred_alert(
            origin, remediation, cs.secret_readers_failed
        ),
    )


def narrow_tags_for(
    tools: DeployTools, config: Config, target: TickTarget, role: str
) -> frozenset[str] | None:
    """The narrowest tags `role`'s own change needs, or None when nothing could narrow it.

    An EMPTY set is the derivation's answer that the range reaches no host through `role`
    (#4361), and `record` writes no line for it (#4408). It takes both halves of the answer,
    exit `NARROW_SETUP_NOTHING_EXIT` and stdout `NARROWED_TO_NOTHING`, for the reason
    `deploy_narrow._one_setup_role` gives: the script and this copy can be different ages.

    The role tag applies far more than any one change to it needs: `--tags k3s` reapplies
    MetalLB, Longhorn, the crons, CoreDNS and the node config and arms two gated
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
        log(
            f"narrow-setup: {role} not narrowed ({type(exc).__name__}: {exc})",
            level=WARNING,
        )
        return None
    if (
        rc == deploy_narrow.NARROW_SETUP_NOTHING_EXIT
        and out == deploy_narrow.NARROWED_TO_NOTHING
    ):
        return frozenset()
    if rc != 0:
        return None
    return frozenset(tag for tag in out.split(",") if tag) or None


class Recorded(NamedTuple):
    """What one broad tick wrote, and everything `unrecord` needs to take it back.

    Attributes:
        roles: the roles whose `manual_plane` LINE this tick appended. A role a previous tick
            recorded keeps its first-seen stamp and is not in here.
        tags_before: role tag -> its tags as they stood BEFORE this tick, or None where the
            role was not pending. One entry per role the tick wrote a row for,
            which is every role it was handed — including the ones already pending.
    """

    roles: list[str]
    tags_before: dict[str, frozenset[str] | None]


def nothing_recorded() -> Recorded:
    """What `handle_broad` passes to `unrecord` for a tick that records no role.

    A fresh value per call rather than a module constant, so no caller can share the list.
    `unrecord` still drops the tick's receipt for it, which is #2382's fix: the range the bug
    is reported on — an applyable setup plane, then a busy lock in a later plan — is exactly
    the one with nothing pending.
    """
    return Recorded([], {})


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

    The narrow tags go on the ledger line of EVERY role in `roles`, not only the ones this
    tick added: a second range touching an already-pending role adds work the first line
    cannot describe, and `record_manual_plane_tags` unions the two. They go in this origin's
    receipt as well, scoped to this range alone, which is what `land.sh` quotes (#3391).

    The journal line names only what this tick ADDED: `main()` logs the whole pending set.

    A role the range reaches no host through (`narrow_tags_for` answers an empty set) gets no
    line, no tags and no page, so no marker asks a human to apply nothing (#4408). The
    derivation therefore runs before anything is written. Its receipt row is
    `NARROWED_TO_NOTHING`, the token the `applied` half already uses for "no play ran": an
    empty row there means "no derivation could narrow it", so `land.sh` would print the
    whole-role tag and a clear command for a line that does not exist.

    Returns:
        A `Recorded` carrying each half of what this tick did, because they need different
        reverses (#2320). The lines it appended are dropped outright. The rows it WIDENED
        belong to a role an earlier range made pending, so they are put back to the snapshot
        taken here — the union is not invertible, and dropping the line with them would take
        back a range that is still merged.
    """
    now = time.time()
    origin = target.origin
    narrowed = {
        setup_role_tag(role): narrow_tags_for(tools, config, target, role)
        for role in roles
    }
    idle = [role for role in roles if narrowed[setup_role_tag(role)] == frozenset()]
    if idle:
        log(
            f"manual_plane: {', '.join(idle)} needs no tag over this range, so not recording it"
        )
    state.record_receipt(
        origin,
        target.local,
        manual={
            tag: tags
            if tags != frozenset()
            else frozenset({deploy_narrow.NARROWED_TO_NOTHING})
            for tag, tags in narrowed.items()
        },
    )
    roles = [role for role in roles if role not in idle]
    if not roles:
        return nothing_recorded()
    before = state.manual_plane_tags_pending()
    tags_before = {
        setup_role_tag(role): before.get(setup_role_tag(role)) for role in roles
    }
    recorded = [
        role
        for role in roles
        if state.record_manual_plane(
            origin,
            _ledger_playbook(role),
            setup_role_tag(role),
            now,
            setup_role_host(role),
            setup_role_agent_hosts(role),
        )
    ]
    for role in roles:
        tag = setup_role_tag(role)
        state.record_manual_plane_tags(tag, narrowed[tag], role not in recorded)
    narrow = state.manual_plane_tags_pending()
    if recorded:
        log(
            f"manual_plane: recorded {', '.join(recorded)} — merged, not applied; "
            + manual_plane_remediation(set(recorded), narrow)
        )
    deploy_alerts.alert_once(
        tools,
        state,
        config,
        "broad",
        origin,
        deploy_alert_text.manual_plane_alert(
            origin,
            manual_plane_remediation(set(roles), narrow),
            state.path("owed"),
        ),
    )
    return Recorded(recorded, tags_before)


def _ledger_playbook(role: str) -> str:
    """The `playbook` a role's `manual_plane` line records: a path, `NO_PLAYBOOK` or unrouted.

    An unrouted role is keyed by its directory name, which `setup_role_tag` falls back to and
    `gitops_state.marker_key` matches as well as the tag.
    """
    if not is_routed(role):
        return UNROUTED_PLAYBOOK
    return setup_role_playbook(role) or NO_PLAYBOOK


def unrecord(state: DeployerState, origin: str, recorded: Recorded) -> None:
    """Take back everything `record` wrote, for a tick whose ff-merge was undone.

    A pending role means merged-and-unapplied. When the tick resets to `local` — which
    `for_contention` does, because nothing was applied — the merge half stops being true, so
    the marker would page for six hours about work no tree carries. The dedupe page is cleared
    with it, but only when it names THIS origin: a page for an earlier SHA is somebody else's.

    THE RECEIPT GOES TOO (#2382). A plan that applied before the reset recorded it, and after
    the reset it names a SHA no tree here carries — which `land.sh` reads as "this tick
    applied that plane". The plane really was applied, and the next tick re-crosses the whole
    range and re-applies it idempotently, so the receipt the reset leaves behind is a claim
    about a range that has to be made again.

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
    state.drop_receipt(origin)
    cleared = {setup_role_tag(role) for role in recorded.roles}
    for tag in cleared:
        state.clear_manual_plane(tag)
    for tag, before in recorded.tags_before.items():
        if tag not in cleared:
            state.restore_manual_plane_tags(tag, before)
    # Only when this tick appended a line. A tick that only widened an already-pending role's
    # row leaves the role pending either way, so clearing the dedupe there re-paged the same
    # SHA on every contended tick.
    if recorded.roles and state.alerted_sha("broad") == origin:
        state.clear_alerted("broad")


def clear_applied(state: DeployerState, playbook: str, tags: list[str]) -> None:
    """Drop the pending roles this apply covered, and say so.

    The deployer's own reverse of `record`, keyed on the `--tags` values the apply ran, never
    on role directories: `chezmoi_setup`'s line is keyed `chezmoi`, the tag that applies it.

    A tag for a role gated off this host is dropped first: `optimize_pi` shares the playbook
    path, so its line would otherwise clear over a run that skipped the role (#3933).

    An `UNROUTED_PLAYBOOK` line names no playbook to match, and is keyed by the role's
    directory. It clears once the routing places that role on this playbook and host, and
    the apply ran its tag.
    """
    tags = [t for t in tags if not tag_selects_an_off_host_role(t)]
    cleared = state.clear_manual_plane_applied(playbook, tags)
    for e in state.manual_plane_pending():
        if (
            e.playbook == UNROUTED_PLAYBOOK
            and tick_applies_setup_role(e.role)
            and setup_role_playbook(e.role) == playbook
            and setup_role_tag(e.role) in tags
            and state.clear_manual_plane(e.role)
        ):
            cleared.append(e.role)
    for role in cleared:
        log(
            f"manual_plane: cleared for {role}: this tick applied {playbook} --tags {role}"
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
        f"manual_plane: pending {', '.join(roles)} — apply by hand: "
        + manual_plane_remediation(set(roles), state.manual_plane_tags_pending())
    )
