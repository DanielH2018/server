# ansible/roles/setup/gitops_deploy/files/gitops_markers.py
"""The deployer's state directory, its marker basenames, and the parsers for their formats.

ONE SOURCE, SHIPPED FROM HERE. Every tree that reads the markers under
`/var/lib/gitops-deploy` gets this file. The deployer runs from `/opt/gitops-deploy`, and the
deploy-ui and renovate-agent roles install this same file into their own `/opt` directories by
path, which `deploy_changes.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` routes so a change here
re-applies all three (#3306). Code that runs from the checkout (`scripts/lib/deployer_park.py`,
`scripts/deploy_tools/gitops_state.py`) imports it through a `sys.path` insert of this
directory. Until issue #2063 each reader restated the directory and the basenames it read, and
three of them parsed the same `contention_since` and `manual_plane` line formats
independently.

monitor-bridge is the one copy. It ships its own `files/` into a pod through a ConfigMap, so
`scripts/dev/gen_gitops_markers.py` writes this file there under a provenance header, and
`ansible/tests/deploy/test_gitops_markers_copies.py` fails when that copy differs from what
the generator writes now. Edit here, run the generator, and commit the copy in the same PR.

Stdlib only and import-free by construction: the pod, the hook and the `/opt` scripts share
nothing else. The formats are the deployer's — `DeployerState` in `deploy_state.py` writes
every one of these files and reads them back through the same functions, so a reader here
sees exactly the shape the writer produced.
"""

import json
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
    # THE OWED-WORK LEDGER (#3392): one JSON object per line, each naming the work a tick left
    # for somebody else under a `class`. See `parse_owed`, and `OWED_CLASSES` for what each
    # class means and who reads it.
    #
    # JSON LINES, BECAUSE THE MARKERS BESIDE IT BROKE ON EVERY NEW FIELD. Each line-format
    # parser above accepts an exact field count and skips anything else, and the reader copies
    # redeploy on their own schedules — so a field a new deployer appended read as NO pending
    # work in an un-redeployed reader. That constraint is why `manual_plane_tags` is a sidecar
    # and why `k8s_unapplied` was a separate file rather than a class tag. `parse_owed` ignores
    # every key it does not know, so a new key is invisible to an old reader instead of
    # erasing the line, and every writer here carries unknown keys through a rewrite.
    #
    # `k8s_unapplied` is the first class to move, because nothing pages on it. The other owed
    # families (`hold_plane`, `manual_plane` and its sidecar, `k8s_deferred`) follow once
    # every reader copy has shipped this parser.
    "owed": "owed.jsonl",
    # The `k8s_unapplied` marker file as it stood before #3392 moved the class into `owed`.
    # Read ONLY by `DeployerState.fold_legacy_k8s_unapplied`, which folds it into the ledger
    # and removes it. Delete this entry and that method once daniel-box has ticked past the
    # fold, as #3075 did for the alert slots.
    "k8s_unapplied_legacy": "k8s_unapplied",
    # One JSON object per line, one line per origin SHA a tick crossed with a broad change
    # (#3391): which planes it applied, with their tags, and which setup roles it left owed to
    # a hand, with the narrowest tags each needs. See `parse_receipts`.
    #
    # `land.sh` READS THIS INSTEAD OF RE-DERIVING. Before it, `narrow_plane` read the
    # `manual_plane_tags` sidecar — whose row spans every range that made a role pending — and
    # re-ran the narrowing over the PR's own range to prove the row covered it. A receipt is
    # scoped to ONE tick's range, so a landing whose merge commit falls inside that range can
    # quote it as it stands. Bounded to `RECEIPT_KEEP` lines, newest last.
    "receipts": "receipts.jsonl",
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
    # The per-SHA alert dedupe slots, ONE file holding every channel as `"<slot> <origin_sha>"`
    # lines (`ALERT_SLOTS`, `parse_alerted`). The operator is paged ONCE per origin SHA about
    # a deferred broad change, a secrets-only push (a rotated value with no service template
    # change), a tasks-only push (a role tasks/ change, not auto-deployed), a k8s-role push (no
    # mechanism here ever applies one, so there is no "rode a redeploy" case to dedupe against
    # `deployed`), a stale denylist (the DISARM itself is stateless and recomputed every tick —
    # only the page is throttled), a master tip that FAILED CI (until the operator fixes or
    # reverts; there is no marker for `ci_pending`, which resolves itself within a tick or two
    # and stays silent) — rather than every tick for as long as the state persists.
    #
    # ONE KEYED FILE RATHER THAN SEVEN `<channel>_alerted_sha` FILES (#3047), and nothing
    # outside the deployer reads it. `dirty_alerted` below is keyed by DATE rather than SHA,
    # and `denylist_rendered` gates a git read rather than a page, so neither is a slot.
    # `deploy_state_alerts.AlertSlotMarkers` reads and writes it; the one-shot migration that
    # folded a host's seven old files in was deleted once daniel-box had ticked past it (#3075).
    "alerted": "alerted_shas",
    # The checkout SHA the denylist reconcile last ran against — the once-per-SHA guard on
    # `deploy_phases.reconcile_denylist`. It bounds BOTH directions: the git read is skipped
    # entirely while the checkout has not moved, and a mismatch a re-render cannot fix (a
    # config rendered from an unpushed tree) re-renders once per SHA rather than every tick.
    "denylist_rendered": "denylist_rendered_sha",
    # The last dirty-alert slot (`YYYY-MM-DD:am|pm`) paged for a dirty working tree. The tick
    # runs every 30 min, so without this an open edit session would re-alert all day; one
    # alert per slot — a morning slot at/after DIRTY_ALERT_MORNING_HOUR (08:00 CT) and an
    # evening slot at/after DIRTY_ALERT_EVENING_HOUR (20:00 CT). See
    # `deploy_logic.dirty_alert_slot`.
    "dirty_alerted": "dirty_alerted_date",
    # The one that is not a per-SHA dedupe marker. It is here for the same reason as the
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
}

# Every alert channel the `alerted` marker dedupes, one slot per channel. The slot IS the
# channel `deploy_alerts.alert_once` keys the `pending_alerts` queue with, so a caller names
# one string rather than a marker and a channel that have to agree.
#
# A NAMED SET, so a typo raises rather than opening a seventh slot — the check
# `DeployerState.path` gave each slot while it was its own `MARKERS` entry.
# `tests/test_alert_once_markers.py` checks every call site's literal against it.
ALERT_SLOTS: frozenset[str] = frozenset(
    {"broad", "secrets", "tasks", "k8s", "stale_denylist", "ci"}
)

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


def parse_alerted(marker: str | None) -> dict[str, str]:
    """The SHA each alert slot last paged on, by slot, from the `alerted` marker.

    A slot with no line has paged on nothing, which is what `.get(slot)` returns — the answer
    a missing `<channel>_alerted_sha` file gave before #3047 collapsed the seven.

    A line this cannot parse is SKIPPED: a torn line read as a SHA would suppress the page for
    a SHA nobody was told about, the one direction a dedupe marker must not fail in.
    """
    out: dict[str, str] = {}
    for line in (marker or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        out[parts[0]] = parts[1]
    return out


def format_alerted(alerted: dict[str, str]) -> str | None:
    """The `alerted` marker for a slot -> SHA mapping, or None when it is empty.

    The reverse of `parse_alerted`, beside it so the two cannot drift. Sorted by slot, so two
    ticks writing the same slots write the same bytes.
    """
    if not alerted:
        return None
    return "\n".join(f"{slot} {alerted[slot]}" for slot in sorted(alerted))


def parse_k8s_deferred(marker: str | None) -> list[K8sDeferredEntry]:
    """Every pending line of `k8s_deferred`, in the order they stand.

    The pre-#3392 `k8s_unapplied` file shares this format, so `k8s_unapplied_entries` reads it
    with this parser until the deployer has folded it into the `owed` ledger.

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
    """The service a RAW `k8s_deferred` line names, or None for none at all.

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


def rewrite_k8s_lines(marker: str | None, services, now: float) -> str:
    """`marker`'s text with every TORN line naming one of `services` made readable.

    The repair keeps the line's first-seen stamp, which is the age a reader dates the change
    from: its own where that field reads as one, `now` where it does not (#2657). A readable
    line is carried as it stands — `k8s_deferred` keeps its recorded origin. The ledger's
    `rewrite_owed` is the same rewrite with an `advance`, which `k8s_unapplied` wants.

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
        now: the stamp a repaired line takes when its own field reads as nothing.

    Returns:
        The text, rewritten. Compare it with the original to see whether anything changed.
    """
    wanted = set(services)
    readable = {entry.service for entry in parse_k8s_deferred(marker)}
    kept = []
    # DECIDED: a repaired line is rewritten to exactly three fields, so a FOURTH field on a
    # line naming one of `services` is discarded rather than carried. Before this, both the
    # record and the clear carried such a line verbatim. Three fields is the format every
    # reader parses, for the reason the `manual_plane_tags` comment above gives — a fourth
    # would read as no pending bump in an un-redeployed monitor-bridge — so nothing may write
    # one, and a line carrying one came from a bug or a hand edit, not from a newer writer.
    for line in (marker or "").splitlines():
        service = k8s_line_service(line)
        if service is None or service not in wanted:
            kept.append(line)
        elif parse_k8s_deferred(line):
            kept.append(line)
        elif service not in readable:
            readable.add(service)
            kept.append(f"{line.split()[0]} {service} {k8s_line_stamp(line, now)}")
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


# ── the owed-work ledger (#3392) ─────────────────────────────────────────────────────────

# A k8s role change — hand-edited or denylisted — that a tick fast-forwarded and will never
# apply. The subject is the service, under the `--tags` value that selects it.
#
# NOTHING PAGES ON THIS CLASS, by construction rather than by omission (#2570): forty of the
# fifty-four k8s roles are denylisted (`docs/reference/decisions.md`), so a page on their
# ordinary changes would hold GitOps Deploy — Status red as normal operation. Its readers are
# the SessionStart banner and the deployer's own journal, both read by a person who is
# already looking.
#
# THE ENTRY DISCHARGES ITSELF. A denylisted role is one this deployer never applies, so an
# entry with only a deployer-side clear would accumulate one per routine landing. Every tick
# asks, per entry, whether the service's release record
# (`roles/k8s/manifests/tasks/release_stamp.yml`) names a commit that CONTAINS the recorded
# SHA (`deploy_defer.discharge_k8s_unapplied`), which discharges an operator's own
# `deploy.sh` too.
OWED_K8S_UNAPPLIED = "k8s_unapplied"

# Every class a ledger line may carry. A reader asks for its classes by name and never sees
# the rest, which is what lets a new class ship before every reader knows it.
OWED_CLASSES: frozenset[str] = frozenset({OWED_K8S_UNAPPLIED})


class OwedEntry(NamedTuple):
    """One readable line of the `owed` ledger.

    Attributes:
        cls: the class, one of `OWED_CLASSES` from a writer this tree knows.
        subject: what is owed, under the `--tags` value that selects it.
        origin: the origin SHA the work was recorded at.
        at: when it was first recorded, in `time.time()` terms. The age every reader dates
            the work from, so a rewrite never refreshes it.
    """

    cls: str
    subject: str
    origin: str
    at: float


def _json_object(line: str) -> dict | None:
    """`line` as a JSON object, or None for anything else — blank, torn, or not an object."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _owed_entry(obj: dict | None) -> OwedEntry | None:
    """The entry one decoded ledger line carries, or None where a required key is missing.

    Every key beyond the four is IGNORED rather than refused. That is the property the ledger
    exists for: the line formats above skip a line with one field too many, so a writer that
    added a field silenced every reader that had not redeployed yet.
    """
    if obj is None:
        return None
    cls, subject, origin, at = (
        obj.get(k) for k in ("class", "subject", "origin", "at")
    )
    if not (
        isinstance(cls, str) and isinstance(subject, str) and isinstance(origin, str)
    ):
        return None
    if not (cls and subject and origin):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return OwedEntry(cls, subject, origin, float(at))


def parse_owed(text: str | None, cls: str | None = None) -> list[OwedEntry]:
    """Every readable line of the `owed` ledger, in the order they stand.

    Args:
        text: the ledger's contents, or None for an absent ledger.
        cls: keep only this class. None keeps every class, including ones this tree has never
            heard of.

    A line that is not a JSON object, or lacks one of `class`, `subject`, `origin` and `at`, is
    SKIPPED, for the reason every parser here skips one: a page raised off a torn line names
    nothing and cannot be cleared. The writers carry such a line through untouched.
    """
    entries = []
    for line in (text or "").splitlines():
        entry = _owed_entry(_json_object(line))
        if entry is not None and (cls is None or entry.cls == cls):
            entries.append(entry)
    return entries


def owed_line(cls: str, subject: str, origin: str, at: float, **extra) -> str:
    """One ledger line. Keys sorted, so two writers recording the same entry write one string."""
    return json.dumps(
        {**extra, "class": cls, "subject": subject, "origin": origin, "at": int(at)},
        sort_keys=True,
    )


def owed_line_key(line: str) -> tuple[str, str] | None:
    """The `(class, subject)` a RAW ledger line names, or None where it names nothing.

    A line missing its `origin` or `at` is still ATTRIBUTABLE, and a writer repairs it rather
    than appending a second line beside it (#2657). A line naming no class and subject is
    carried untouched by every writer: dropping it loses the only record that something was
    owed, and nothing can say what.
    """
    obj = _json_object(line)
    if obj is None:
        return None
    cls, subject = obj.get("class"), obj.get("subject")
    if isinstance(cls, str) and cls and isinstance(subject, str) and subject:
        return cls, subject
    return None


def rewrite_owed(
    text: str | None, cls: str, subjects, origin: str, now: float, advance: bool
) -> str:
    """`text` with every `cls` line naming one of `subjects` brought up to date.

    The ledger's form of `rewrite_k8s_lines`, with one difference: every key a line carries
    beyond the four survives the rewrite, because a newer writer may have put it there.

    Two rewrites, and both keep the line's first-seen stamp. A TORN LINE IS MADE READABLE
    (#2657), taking its own `at` where that reads as a number and `now` where it does not.
    Under `advance`, a readable line moves to `origin` (#2644). A torn line BESIDE a readable
    one for the same subject is dropped, since repairing it would duplicate that line.

    Returns:
        The text, rewritten. Compare it with the original to see whether anything changed.
    """
    wanted = set(subjects)
    readable = {e.subject for e in parse_owed(text, cls)}
    kept = []
    for line in (text or "").splitlines():
        key = owed_line_key(line)
        if key is None or key[0] != cls or key[1] not in wanted:
            kept.append(line)
            continue
        obj = _json_object(line) or {}
        entry = _owed_entry(obj)
        if entry is not None:
            if advance and entry.origin != origin:
                obj["origin"] = origin
                kept.append(json.dumps(obj, sort_keys=True))
            else:
                kept.append(line)
        elif key[1] not in readable:
            readable.add(key[1])
            at = obj.get("at")
            stamp = (
                at if isinstance(at, (int, float)) and not isinstance(at, bool) else now
            )
            first = obj.get("origin")
            if advance or not (isinstance(first, str) and first):
                first = origin
            extra = {
                k: v
                for k, v in obj.items()
                if k not in ("class", "subject", "origin", "at")
            }
            kept.append(owed_line(cls, key[1], first, stamp, **extra))
    return "\n".join(kept)


def drop_owed(text: str | None, cls: str, subjects) -> tuple[str, list[str]]:
    """`text` without its `cls` lines naming any of `subjects`, and the subjects dropped.

    A TORN line naming one of `subjects` goes too (#2657); a line naming nobody stays, for the
    reason `owed_line_key` gives.
    """
    wanted = set(subjects)
    kept, dropped = [], set()
    for line in (text or "").splitlines():
        key = owed_line_key(line)
        if key is not None and key[0] == cls and key[1] in wanted:
            dropped.add(key[1])
            continue
        kept.append(line)
    return "\n".join(kept), sorted(dropped)


def k8s_unapplied_entries(
    owed: str | None, legacy: str | None = None
) -> list[K8sDeferredEntry]:
    """Every pending `k8s_unapplied` change, from the ledger and the pre-#3392 file.

    `legacy` is the `k8s_unapplied_legacy` marker. A host whose deployer has not yet folded it
    into the ledger still holds its lines there, so a reader unions the two; a service the
    ledger already names is read from the ledger. Returned as `K8sDeferredEntry` because every
    caller reads `.service`, `.origin` and `.at` off it.
    """
    entries = [
        K8sDeferredEntry(e.origin, e.subject, e.at)
        for e in parse_owed(owed, OWED_K8S_UNAPPLIED)
    ]
    named = {e.service for e in entries}
    entries += [e for e in parse_k8s_deferred(legacy) if e.service not in named]
    return entries


# ── the per-SHA tick receipt (#3391) ─────────────────────────────────────────────────────

# How many receipts the `receipts` marker keeps. A landing reads the receipt for its own
# merge commit within one or two ticks, and a broad range is a few a day at most, so this is
# days of history in a file of a few kilobytes.
RECEIPT_KEEP = 50


class Receipt(NamedTuple):
    """What one tick did with one origin SHA's broad change.

    Attributes:
        origin: the origin SHA the tick crossed to.
        base: the commit the checkout stood on before it. The range is `base..origin`.
        applied: playbook -> the `--tags` it applied with, for each plane the tick APPLIED.
            An empty tuple is a whole-playbook apply.
        manual: setup role tag -> the narrowest tags its change in this range needs, for
            each role the tick left owed to a hand. An empty frozenset means no derivation
            could narrow it, so the reader prints the whole-role tag.
    """

    origin: str
    base: str
    applied: dict[str, tuple[str, ...]]
    manual: dict[str, frozenset[str]]


def _receipt(obj: dict | None) -> Receipt | None:
    """The receipt one decoded line carries, or None where its shape is not a receipt.

    Unknown keys are ignored, as `parse_owed` ignores them, and for the same reason.
    """
    if obj is None:
        return None
    origin, base = obj.get("origin"), obj.get("base")
    applied, manual = obj.get("applied", {}), obj.get("manual", {})
    if not (isinstance(origin, str) and origin and isinstance(base, str)):
        return None
    if not (isinstance(applied, dict) and isinstance(manual, dict)):
        return None
    if not all(isinstance(v, list) for v in [*applied.values(), *manual.values()]):
        return None
    return Receipt(
        origin,
        base,
        {k: tuple(str(t) for t in v) for k, v in applied.items()},
        {k: frozenset(str(t) for t in v) for k, v in manual.items()},
    )


def parse_receipts(text: str | None) -> list[Receipt]:
    """Every readable receipt in the `receipts` marker, oldest first. Torn lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        receipt = _receipt(_json_object(line))
        if receipt is not None:
            out.append(receipt)
    return out


def receipt_line(receipt: Receipt) -> str:
    """One `receipts` line, the reverse of `parse_receipts` for a single receipt."""
    return json.dumps(
        {
            "origin": receipt.origin,
            "base": receipt.base,
            "applied": {k: list(v) for k, v in sorted(receipt.applied.items())},
            "manual": {k: sorted(v) for k, v in sorted(receipt.manual.items())},
        },
        sort_keys=True,
    )
