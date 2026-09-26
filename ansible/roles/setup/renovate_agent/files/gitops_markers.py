# ansible/roles/setup/renovate_agent/files/gitops_markers.py
# generated_from: ansible/roles/setup/gitops_deploy/files/gitops_markers.py -- do not edit.
# A verbatim copy written by scripts/dev/gen_gitops_markers.py; edit the source, run it, and
# commit every copy in the same PR.
"""The deployer's state directory, its marker basenames, and the parsers for their formats.

THIS FILE IS COPIED, NOT IMPORTED ACROSS TREES. Five trees read the markers under
`/var/lib/gitops-deploy` and none of them may import another: the deployer runs from
`/opt/gitops-deploy`, monitor-bridge ships its `files/` into a pod, deploy-ui and
renovate-agent run from their own `/opt` directories, and `scripts/lib/deployer_park.py` is
imported by the SessionStart hook before anything else is on `sys.path`. Until issue #2063
each restated the directory and the basenames it read, and three of them parsed the same
`contention_since` and `manual_plane` line formats independently, held together after the
fact by `ansible/tests/deploy/test_*_parsers_agree.py`.

This is the one hand-edited source. `scripts/dev/gen_gitops_markers.py` writes a copy with a
provenance header into each consumer's tree — `COPIES` there is the list — and
`ansible/tests/deploy/test_gitops_markers_copies.py` fails when a committed copy differs from
what the generator writes now, the way `test_every_committed_fragment_matches_what_the_generator_writes_now`
keeps the docs fragments fresh. Edit here, run the generator, commit every copy in the same
PR.

Stdlib only and import-free by construction: the pod, the hook and the `/opt` scripts share
nothing else. The formats are the deployer's — `DeployerState` in `deploy_state.py` writes
every one of these files and reads them back through the same functions, so a reader here
sees exactly the shape the writer produced.
"""

from typing import NamedTuple

STATE_DIR = "/var/lib/gitops-deploy"

# Marker name -> basename on disk. THE table of what lives in the state directory: the
# deployer reads and writes every marker through it, every other tree names a file through it,
# and `tests/test_deployer_state.py` pins every pair by name. What each file records, and why
# it exists, is beside its entry.
MARKERS: dict[str, str] = {
    # The SHA whose deploy failed its health gate or broad apply; the host is HELD there until
    # an operator clears it (`write_hold`, `clear_broad_hold`, `clear_service_hold`).
    "hold": "hold_sha",
    # The playbook (and tags) whose broad apply failed, written beside `hold_sha`. That marker
    # alone is service-shaped — monitor-bridge's message says "revert the offending PR", the
    # wrong remediation for a broad apply: the tree is already fast-forwarded and a playbook is
    # what broke, so reverting the PR undoes nothing. This names what to re-run instead.
    "hold_plane": "hold_plane",
    # The last broad plane this host APPLIED, as `<origin_sha> <playbook> <tags>`. The only
    # durable evidence that a tick applied a plane, as against fast-forwarding past it:
    # `behind_since` empty says local == origin, which any session's `git merge --ff-only`
    # also produces, and after that `next_action()` returns `noop` forever so the plane is
    # stranded (issue #1537). Read by `land.sh` before it says `settled`.
    "broad_applied": "broad_applied",
    # One line per setup role this host fast-forwarded past and cannot apply itself,
    # `"<origin_sha> <playbook-or-none> <role> <unix_ts>"`. See `parse_manual_plane`.
    "manual_plane": "manual_plane",
    # The narrowest `--tags` value each pending role's own change actually needs, one line per
    # role as `"<role> <tag,tag>"` or `"<role> -"` for a range no derivation could narrow.
    # See `parse_manual_plane_tags`.
    #
    # A SIDECAR RATHER THAN A FIFTH FIELD ON THE LINE ABOVE. `parse_manual_plane` accepts
    # exactly four fields and SKIPS anything else, and these copies reach their hosts one role
    # deploy at a time — so a five-field line written by a new deployer would read as no
    # pending role at all in an un-redeployed monitor-bridge, and the page it raises on a
    # role's age would stop firing. A reader that has never heard of this file degrades to the
    # whole-role tag, which is the blunt but correct answer it printed before #2307.
    "manual_plane_tags": "manual_plane_tags",
    # `"<origin_sha> <lock> <unix_ts_first_seen> <unix_ts_last_seen> <count>"` while
    # consecutive ticks defer on one busy service lock. See `parse_contention`.
    "contention": "contention_since",
    # One line per promoted k8s image bump a BROAD tick fast-forwarded and then deferred for
    # lack of budget, `"<origin_sha> <service> <unix_ts>"`. See `parse_k8s_deferred`.
    #
    # Scoped to that one deferral (#2449). The tick has already merged the bump, so no later
    # tick's range carries it again, and the defer-and-alert post names it exactly once. Every
    # other k8s defer-and-alert change — a hand-edited role, a denylisted one — is merged by a
    # person who is landing it and can deploy it, and forty of the fifty-four k8s roles are
    # denylisted, so recording those would hold GitOps Deploy — Status red as normal operation.
    # A budget deferral has no such person: nothing chose it, and nothing reports it again.
    "k8s_deferred": "k8s_deferred",
    # One line per HAND-EDITED or DENYLISTED k8s role change a tick fast-forwarded and will
    # never apply, in the same `"<origin_sha> <service> <unix_ts>"` format as `k8s_deferred`.
    # See `parse_k8s_deferred`, which reads both.
    #
    # A SEPARATE FILE RATHER THAN A FOURTH FIELD ON THE LINE ABOVE, and rather than a fourth
    # arm in `gitops_status` (#2570). `parse_k8s_deferred` accepts exactly three fields and
    # skips anything else, so a class tag appended to a `k8s_deferred` line would read as NO
    # pending bump in an un-redeployed monitor-bridge and silence the page that marker exists
    # to raise — the failure `manual_plane_tags` was made a sidecar to avoid. A reader that
    # has never heard of this basename ignores it, which is exactly the posture this class
    # wants: NOTHING PAGES ON THIS MARKER. `gitops_status` does not read it, by construction
    # and not by omission. Forty of the fifty-four k8s roles are denylisted
    # (`docs/reference/decisions.md`), so a page on their ordinary changes would hold GitOps
    # Deploy — Status red as normal operation. The durable readers are the SessionStart banner
    # and the deployer's own journal, both of which a person reads when they are already
    # looking.
    #
    # THE LINE DISCHARGES ITSELF. A denylisted role is by definition one this deployer never
    # applies, so a marker with only a deployer-side clear would accumulate one line per
    # routine landing and rebuild the always-red tile on the banner. Every tick therefore
    # asks, per pending line, whether the service's release record
    # (`roles/k8s/manifests/tasks/release_stamp.yml`) now names a commit that CONTAINS the
    # recorded SHA — one `git merge-base --is-ancestor`, which is a different question from
    # `probe.py releases --stale-only`'s path comparison and cannot drift against it. That
    # discharges an operator's own `deploy.sh`, which the deployer cannot otherwise see.
    "k8s_unapplied": "k8s_unapplied",
    # The unix time the last tick completed; monitor-bridge's GitOps Alive reads its age.
    "last_run": "last_run",
    # Origin SHA recorded while local and origin have DIVERGED (`deploy_logic.is_diverged`):
    # the deployer can't fast-forward and noops forever, so origin's new commits never deploy
    # while both GitOps monitors stay green. monitor-bridge reads this off the same :ro mount
    # as `hold_sha` and pages GitOps Status until the host tree is reconciled.
    "diverged": "diverged_sha",
    # `"<origin_sha> <unix_ts_first_seen>"` while the host is BEHIND origin at the end of a
    # tick — origin strictly ahead and we did not converge. Every reason lands here: a
    # deferred broad change, a long-dirty tree, a hold. The broad path in particular is
    # invisible otherwise — it never ff-merges, so the host parks behind master indefinitely
    # while `last_run` keeps ticking (Alive green) and `is_diverged` stays false (origin is a
    # strict descendant, so Status green too). That is how daniel-server sat on a
    # 12-commit-old tree for hours on 2026-08-02 with every GitOps signal green, until the
    # un-deployed Pi-hole DNS records were noticed by hand.
    #
    # The timestamp is what makes this safe to page on: a normal push is behind for one tick,
    # and an operator mid-edit (the dirty path, deliberately treated as healthy) is behind for
    # as long as they are editing. Only sustained behind-ness is a problem, so every reader
    # applies an age threshold. The stamp is renewed by any tick that fast-forwarded and kept
    # by one that moved nothing, so its age is HOW LONG THE DEPLOYER HAS NOT FAST-FORWARDED —
    # not how long the host has been behind the tip: a deployer landing every merge at the
    # newest green ancestor is behind the tip on nearly every tick, and only one that stops
    # moving ages this. See `parse_behind` and `DeployerState.record_behind`.
    "behind": "behind_since",
    # The sorted stale-compose set last alerted on, so a lingering stale dir doesn't re-page
    # every tick — only a CHANGED set (new stale dir, or one cleaned up) re-alerts.
    "stale_composes": "stale_composes_alerted",
    # Per-SHA dedupe markers, one per alert channel: the operator is paged ONCE per origin SHA
    # about a deferred broad change, a secrets-only push (a rotated value with no service
    # template change), a tasks-only push (a role tasks/ change, not auto-deployed), a
    # meta-only push (a role meta/deps.yml change — the cross-service deploy graph), a
    # k8s-role push (no mechanism here ever applies one, so there is no "rode a redeploy" case
    # to dedupe against `deployed`), a stale denylist (the DISARM itself is stateless and
    # recomputed every tick — only the page is throttled), a master tip that FAILED CI (until
    # the operator fixes or reverts; there is no marker for `ci_pending`, which resolves
    # itself within a tick or two and stays silent), and a staging-gate verdict — rather than
    # every tick for as long as the state persists.
    "broad_alerted": "broad_alerted_sha",
    "secrets_alerted": "secrets_alerted_sha",
    "tasks_alerted": "tasks_alerted_sha",
    "meta_alerted": "meta_alerted_sha",
    "k8s_alerted": "k8s_alerted_sha",
    "stale_denylist_alerted": "stale_denylist_alerted_sha",
    # The checkout SHA the denylist reconcile last ran against — the once-per-SHA guard on
    # `deploy_phases.reconcile_denylist`. It bounds BOTH directions: the git read is skipped
    # entirely while the checkout has not moved, and a mismatch a re-render cannot fix (a
    # config rendered from an unpushed tree) re-renders once per SHA rather than every tick.
    "denylist_rendered": "denylist_rendered_sha",
    "ci_alerted": "ci_alerted_sha",
    "staging_alerted": "staging_alerted_sha",
    # The last dirty-alert slot (`YYYY-MM-DD:am|pm`) paged for a dirty working tree. The tick
    # runs every 30 min, so without this an open edit session would re-alert all day; one
    # alert per slot — a morning slot at/after DIRTY_ALERT_MORNING_HOUR (08:00 CT) and an
    # evening slot at/after DIRTY_ALERT_EVENING_HOUR (20:00 CT). See
    # `deploy_logic.dirty_alert_slot`.
    "dirty_alerted": "dirty_alerted_date",
    # The three that are not per-SHA dedupe markers. They are here for the same reason as the
    # rest — so a caller names a marker rather than carrying a path — and because the
    # `state_dir` fixture repoints the whole `DeployerState` at once, which a path threaded
    # through a function argument would escape.
    #
    # Undelivered post-merge alerts, retried at the TOP of every tick. The
    # secrets/tasks/meta/combined channels `git merge --ff-only` BEFORE their delivery-gated
    # marker write, so once merged local==origin and the next tick short-circuits at `noop`
    # (main) before ever re-reaching the alert code — a single transient discord() failure
    # (timeout/5xx/Cloudflare-1010/DNS blip) would otherwise drop that alert forever (the
    # rotated secret sits stale in its container / the tasks|meta change sits
    # ff-merged-but-unapplied, with no other signal). This queue decouples DELIVERY from the
    # git action: an alert that fails to send is persisted here keyed by "<channel>:<sha>"
    # and `drain_pending()` resends it every tick until a confirmed 2xx clears it. The
    # per-SHA markers above still gate DETECTION (so a delivered alert isn't re-queued on the
    # broad path's every-tick re-eval); this queue owns delivery.
    "pending_alerts": "pending_alerts.json",
    # Where a real gated tick's verdict is recorded (docs/staging-phase-c.md, *After the flip:
    # the tick ledger*). Nothing reads it back since the staging-backfill ratchet was retired
    # (#2414); an operator reads it with `jq`.
    "staging_ticks": "staging-ticks.jsonl",
    # The operator's one-tick escape hatch, armed by creating the file and disarmed by
    # removing it. Decision 4: "Build the override before the gate. A gate with no escape
    # hatch becomes a gate somebody deletes at 2 AM, and nobody reviews the deletion."
    #
    # It is CONSUMED at the point the gate would block, never at the point it is read.
    # Consuming on entry would spend it on the first tick after arming — which is usually a
    # tick with nothing to gate — and leave the operator's actual push facing the block with
    # the hatch already gone.
    "staging_override": "staging_gate_override",
}

# What the playbook field of a `manual_plane` line holds for a role no playbook applies
# (`common`).
NO_PLAYBOOK = "none"

# What the tag field of a `manual_plane_tags` line holds when no derivation could narrow the
# role's change, so the reader prints the whole-role tag. A literal rather than an empty
# field: a line ending in whitespace splits to one part, which every parser here skips.
NARROWED_TO_ROLE = "-"

# What an operator runs to clear one role's `manual_plane` line after applying it by hand,
# and to end a contention streak once the lock's holder is gone. The deployer's alert,
# `land.sh`, monitor-bridge's page and the SessionStart banner all print these; one string
# each so they cannot name four different commands.
MANUAL_PLANE_CLEAR_CMD = (
    "uv run python scripts/deploy_tools/gitops_state.py clear-manual-plane <role>"
)
CONTENTION_CLEAR_CMD = (
    "uv run python scripts/deploy_tools/gitops_state.py clear-contention"
)
K8S_DEFERRED_CLEAR_CMD = (
    "uv run python scripts/deploy_tools/gitops_state.py clear-k8s-deferred <service>"
)


K8S_UNAPPLIED_CLEAR_CMD = (
    "uv run python scripts/deploy_tools/gitops_state.py clear-k8s-unapplied <service>"
)


def k8s_deferred_clear_cmd(service: str = "<service>") -> str:
    """The clear command to print beside a deferred bump's own deploy command."""
    return K8S_DEFERRED_CLEAR_CMD.replace("<service>", service)


def k8s_unapplied_clear_cmd(service: str = "<service>") -> str:
    """The clear command for a merged-and-unapplied k8s role change.

    A hand clear exists even though the marker discharges itself off the release record: a
    role whose change was reverted rather than deployed never gets a record naming it, and a
    line nobody can drop is a banner entry forever.
    """
    return K8S_UNAPPLIED_CLEAR_CMD.replace("<service>", service)


def k8s_deferred_deploy_cmd(services) -> str:
    """The deploy an operator runs to apply the deferred bumps in `services`.

    `./scripts/deploy.sh` rather than a bare `ansible-playbook`: the wrapper takes the service
    lock the tick takes, and a budget deferral means the tick ran out of wall clock, not that
    the pin is bad.
    """
    return './scripts/deploy.sh --tags "%s"' % ",".join(sorted(services))


def manual_plane_clear_cmd(role: str = "<role>", tags=()) -> str:
    """The clear command to print beside an apply of `role` with `tags`.

    A bare clear drops the role's whole line, which is right after a WHOLE-ROLE apply and
    wrong after a narrowed one: a second range can widen the row between the moment a surface
    prints the command and the moment an operator runs it, and the bare form would then drop
    a tag nobody applied (#2349). So a narrowed apply prints `--applied <those tags>`, and the
    clear keeps whatever the row has gained since.

    Args:
        role: the role, under the `--tags` value that selects it. The default is the
            placeholder the generic multi-role remediation prints.
        tags: the tags the apply command names. Empty, or just the role tag, is a whole-role
            apply and gets the bare form.
    """
    cmd = MANUAL_PLANE_CLEAR_CMD.replace("<role>", role)
    applied = [t for t in sorted(frozenset(tags)) if t != role]
    return f"{cmd} --applied {','.join(applied)}" if applied else cmd


# The setup roles whose whole-role tag is a blunt instrument, and the narrowed tags that still
# reach the tasks that make it one. `--tags k3s` reapplies MetalLB, Longhorn, the backup
# targets, the crons, CoreDNS and the node config; every task in `roles/setup/k3s/tasks/
# server.yml` carries `k3s_server`, so a narrowing that lands on that tag arms the installer
# restart and the key re-encryption as surely as the whole-role tag does.
#
# The DATA lives here because three surfaces need it and two of them cannot reach the
# deployer's tree: `deploy_remediation` composes the long prose for the journal, the Discord
# alert and `land.sh`, while `scripts/lib/deployer_park.py` renders the SessionStart banner
# with only `scripts/` on `sys.path` (#2345). `test_the_gated_tags_are_every_tag_the_gated_
# tasks_carry` derives the set from the role itself.
MAXIMAL_ROLE_GATED_TAGS: dict[str, frozenset[str]] = {
    "k3s": frozenset({"k3s_server"}),
}

# The one-line version of what such an apply arms, for a surface with no room for the prose.
# It names `rotate-keys` because that is the irreversible half: the restart costs a few
# seconds of API server, the re-encryption rewrites every Secret in etcd.
MAXIMAL_ROLE_WARNING: dict[str, str] = {
    "k3s": (
        "that command restarts the k3s control plane and arms `k3s secrets-encrypt "
        "rotate-keys`, which re-encrypts every Secret in etcd"
    ),
}


def maximal_apply_warning(role: str, tags) -> str:
    """What a printed `--tags` value for `role` arms, or "" when it arms nothing gated.

    Args:
        role: the role, under the `--tags` value that selects it.
        tags: the tags the surface is about to print, as an iterable of strings.

    Two shapes carry the warning. The WHOLE-role tag, which is what a surface prints when the
    deployer could not narrow the change. And a narrowed list that still reaches the role's
    gated tasks — `MAXIMAL_ROLE_GATED_TAGS`. A narrowed list reaching neither gets nothing:
    a warning printed beside every command is one nobody reads.
    """
    if role not in MAXIMAL_ROLE_WARNING:
        return ""
    tags = frozenset(tags)
    gated = MAXIMAL_ROLE_GATED_TAGS.get(role, frozenset())
    if tags == frozenset({role}) or (tags & gated):
        return MAXIMAL_ROLE_WARNING[role]
    return ""


# How long a contention streak may run before monitor-bridge pages on it and the SessionStart
# banner names it: the deployer's longest apply budget, `gitops_deploy_broad_timeout_s` =
# 1800 s, so a holder past it has outlived every legitimate deploy. The bridge's own default
# for `GITOPS_CONTENTION_MAX_MIN` is derived from this; the rendered env pins the same figure.
CONTENTION_PAGE_SECONDS = 30 * 60


class ManualPlaneEntry(NamedTuple):
    """One pending line of the `manual_plane` marker.

    Attributes:
        origin: the origin SHA whose range first carried this role.
        playbook: the playbook that applies the role, or `NO_PLAYBOOK`.
        role: the role, under the `--tags` value that selects it. The two are the same word
            for every role that can reach this marker, which
            `test_the_marker_key_is_the_role_name_for_every_pending_role` (in
            `test_deployer_state.py`) pins — so an operator clears by the role name the alert
            gives them.
        at: when the deployer first recorded it, in `time.time()` terms. The age this stamp
            gives is what monitor-bridge pages on, so it is NEVER refreshed for a role
            already listed.
    """

    origin: str
    playbook: str
    role: str
    at: float


class K8sDeferredEntry(NamedTuple):
    """One pending line of the `k8s_deferred` marker.

    Attributes:
        origin: the origin SHA whose range carried the bump. The tick merged it, so this is
            the commit an operator's deploy applies.
        service: the k8s service, under the `--tags` value that selects it.
        at: when the deployer first deferred it, in `time.time()` terms. Never refreshed for
            a service already listed, for the reason `ManualPlaneEntry.at` gives.
    """

    origin: str
    service: str
    at: float


class ContentionEntry(NamedTuple):
    """The `contention_since` marker: consecutive ticks deferred on a busy service lock.

    Attributes:
        origin: the origin SHA the most recent deferred tick was trying to reach.
        lock: the lock that stayed busy, as `deploy_locks.ServiceLockBusy.lock` names it.
        first_seen: when the first tick of the streak deferred, in `time.time()` terms. The
            age monitor-bridge and the SessionStart banner read; never refreshed within a
            streak, for the reason `ManualPlaneEntry.at` gives.
        last_seen: when the most recent tick deferred. `entrypoint()` compares it with the
            tick's own start to clear a marker no tick has touched since — a tick that ended
            any other way means the lock stopped wedging the deployer.
        count: how many consecutive ticks deferred.
    """

    origin: str
    lock: str
    first_seen: float
    last_seen: float
    count: int


# Every parser below reads garbage as NOTHING — no park, no streak, no pending role — rather
# than guessing. Each marker's age decides whether something pages or banners, and a page
# raised off a torn line names no lock and no role and cannot be cleared; an operator taught
# that the tile lies stops reading it. The markers are written atomically, so a torn value is
# a bug somewhere, and the writer overwrites or skips such a line rather than carrying it.


def parse_behind(marker: str | None) -> tuple[str, float] | None:
    """The `behind_since` marker as `(origin_sha, first_seen)`, or None when absent or garbled."""
    parts = (marker or "").split()
    if len(parts) != 2:
        return None
    try:
        return parts[0], float(parts[1])
    except ValueError:
        return None


def parse_manual_plane(marker: str | None) -> list[ManualPlaneEntry]:
    """Every pending role in the `manual_plane` marker, oldest line first.

    A line this cannot parse is SKIPPED, never guessed at. `record_manual_plane` and
    `clear_manual_plane` still carry such a line through, so it is skipped, never lost.
    """
    entries = []
    for line in (marker or "").splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        try:
            at = float(parts[3])
        except ValueError:
            continue
        entries.append(ManualPlaneEntry(parts[0], parts[1], parts[2], at))
    return entries


def parse_manual_plane_tags(marker: str | None) -> dict[str, frozenset[str]]:
    """The narrowest tags each pending role needs, by role, from the `manual_plane_tags` marker.

    An EMPTY frozenset means the deployer could not narrow that role's change, so its reader
    prints the whole-role tag. A role with no line at all is the same answer, reached by a
    reader that looked before the sidecar existed or by a tick that wrote none — which is why
    the two are deliberately indistinguishable to a caller using `.get(role, frozenset())`.

    A line this cannot parse is SKIPPED, for the reason every parser here skips: a remediation
    built from a torn line names a tag that selects nothing, and Ansible exits 0 on one.
    """
    out: dict[str, frozenset[str]] = {}
    for line in (marker or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        tags = [t for t in parts[1].split(",") if t and t != NARROWED_TO_ROLE]
        out[parts[0]] = frozenset(tags)
    return out


def format_manual_plane_tags(tags: dict[str, frozenset[str]]) -> str | None:
    """The `manual_plane_tags` marker for a role -> tags mapping, or None when it is empty.

    The reverse of `parse_manual_plane_tags`, here beside it so the two cannot drift: a role
    whose tags are empty is written as `NARROWED_TO_ROLE`, because a line with a trailing
    empty field would split to one part and be skipped as garbled.
    """
    if not tags:
        return None
    return "\n".join(
        f"{role} {','.join(sorted(tags[role])) or NARROWED_TO_ROLE}"
        for role in sorted(tags)
    )


def parse_k8s_deferred(marker: str | None) -> list[K8sDeferredEntry]:
    """Every pending line of `k8s_deferred` OR `k8s_unapplied`, in the order they stand.

    The two markers share this format and this parser, and differ only in who reads them:
    `gitops_status` pages on the first and never opens the second (#2570).

    A line this cannot parse is SKIPPED, never guessed at, for the reason
    `parse_manual_plane` skips one: a page raised off a torn line names no service and cannot
    be cleared.
    """
    entries = []
    for line in (marker or "").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            at = float(parts[2])
        except ValueError:
            continue
        entries.append(K8sDeferredEntry(parts[0], parts[1], at))
    return entries


def k8s_line_service(line: str) -> str | None:
    """The service a RAW `k8s_deferred` / `k8s_unapplied` line names, or None for none at all.

    A torn line is still ATTRIBUTABLE where its second field is there: `"<sha> authelia"` and
    `"<sha> authelia not-a-stamp"` both name authelia, and the writer repairs one rather than
    appending a second line beside it (#2657). A one-word line names nobody, so every caller
    carries it untouched — dropping it loses the only record that something was deferred, and
    nothing can say what.
    """
    parts = line.split()
    return parts[1] if len(parts) >= 2 else None


def k8s_line_stamp(line: str, now: float) -> str:
    """The first-seen stamp a repaired line keeps: its own where it reads as one, else `now`."""
    parts = line.split()
    try:
        return f"{float(parts[2] if len(parts) >= 3 else ''):.0f}"
    except ValueError:
        return f"{now:.0f}"


def rewrite_k8s_lines(
    marker: str | None, services, origin: str, now: float, advance: bool = False
) -> str:
    """`marker`'s text with every line naming one of `services` brought up to date.

    Two rewrites, and both keep the line's first-seen stamp, which is the age a reader dates
    the change from. A TORN LINE IS MADE READABLE (#2657), taking its own stamp where that
    field reads as one and `now` where it does not. Under `advance`, a READABLE line moves to
    `origin` (#2644), which `k8s_unapplied` wants and `k8s_deferred` does not.

    The repair is what stops a writer duplicating a torn line. `parse_k8s_deferred` skips one,
    so a writer reading only its entries sees no line for the service, appends a second, and
    every clear and every discharge — matching on the same three fields — then leaves the torn
    one standing forever. A torn line BESIDE a readable one for the same service is dropped
    instead, since repairing it would duplicate what that line already says.

    Args:
        marker: the raw marker text, or None for an absent marker.
        services: the services the caller is writing about. A line naming anything else is
            carried untouched, as is a line naming nobody — dropping that one loses the only
            record that something was deferred, and nothing can say what.
        origin: the SHA the caller is recording.
        now: the stamp a repaired line takes when its own field reads as nothing.
        advance: move a readable line's SHA to `origin`.

    Returns:
        The text, rewritten. Compare it with the original to see whether anything changed.
    """
    wanted = set(services)
    readable = {entry.service for entry in parse_k8s_deferred(marker)}
    kept = []
    for line in (marker or "").splitlines():
        service = k8s_line_service(line)
        if service is None or service not in wanted:
            kept.append(line)
        elif parse_k8s_deferred(line):
            kept.append(f"{origin} {service} {line.split()[2]}" if advance else line)
        elif service not in readable:
            readable.add(service)
            first = origin if advance else line.split()[0]
            kept.append(f"{first} {service} {k8s_line_stamp(line, now)}")
    return "\n".join(kept)


def parse_contention(marker: str | None) -> ContentionEntry | None:
    """The streak the `contention_since` marker records, or None when absent or garbled."""
    parts = (marker or "").split()
    if len(parts) != 5:
        return None
    try:
        return ContentionEntry(
            parts[0], parts[1], float(parts[2]), float(parts[3]), int(parts[4])
        )
    except ValueError:
        return None
