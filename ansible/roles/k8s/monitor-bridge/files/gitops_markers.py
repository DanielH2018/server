# ansible/roles/k8s/monitor-bridge/files/gitops_markers.py
# generated_from: ansible/roles/setup/gitops_deploy/files/gitops_markers.py -- do not edit.
# A verbatim copy written by scripts/dev/gen_gitops_markers.py; edit the source, run it, and
# commit every copy in the same PR.
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
    # `"<origin_sha> <lock> <unix_ts_first_seen> <unix_ts_last_seen> <count>"` while
    # consecutive ticks defer on one busy service lock. See `parse_contention`.
    "contention": "contention_since",
    # The owed-work ledger (#3392): one JSON object per line, each naming work a tick left for
    # somebody else under a `class`. JSON lines because the line formats here skip a line
    # with one field too many, so a field a new writer appended read as NO pending work in a
    # reader copy that had not redeployed. `gitops_ledger.parse_owed` has the format.
    "owed": "owed.jsonl",
    # One JSON line per origin SHA a tick crossed with a broad change (#3391): the planes it
    # applied and the setup roles it left to a hand. `land.sh` reads it instead of
    # re-deriving. Its `applied` half is the only durable evidence that a tick applied a plane,
    # as against fast-forwarding past it: `behind_since` empty says local == origin, which any
    # session's `git merge --ff-only` also produces, and after that `next_action()` returns
    # `noop` forever, so the plane is stranded (#1537). `gitops_ledger.parse_receipts` has the
    # format.
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

# What the `playbook` key of a `manual_plane` ledger line holds for a role no playbook applies
# (`common`).
NO_PLAYBOOK = "none"

# What an operator runs to clear one owed-ledger line after applying, deploying or reverting
# what it names, and to end a contention streak once the lock's holder is gone. The deployer's
# alert, `land.sh`, monitor-bridge's page, deploy-ui and the SessionStart banner all print
# these; one string each so they cannot name different commands. `clear-owed` is the one
# owed-ledger verb (#3544); every surface prints it (#3547).
OWED_CLEAR_CMD = "uv run python scripts/deploy_tools/gitops_state.py clear-owed"
CONTENTION_CLEAR_CMD = (
    "uv run python scripts/deploy_tools/gitops_state.py clear-contention"
)


def k8s_deferred_deploy_cmd(services) -> str:
    """The deploy an operator runs to apply the deferred bumps in `services`.

    `./scripts/deploy.sh` rather than a bare `ansible-playbook`: the wrapper takes the service
    lock the tick takes, and a budget deferral means the tick ran out of wall clock, not that
    the pin is bad.
    """
    return './scripts/deploy.sh --tags "%s"' % ",".join(sorted(services))


def owed_clear_cmd(cls: str, subject: str, tags=()) -> str:
    """The `clear-owed` command for `subject`'s `cls` line, printed beside what discharges it.

    For `manual_plane`, a bare clear drops the role's whole line, which is right after a
    WHOLE-ROLE apply and wrong after a narrowed one: a second range can widen the row between
    the moment a surface prints the command and the moment an operator runs it, and the bare
    form would then drop a tag nobody applied (#2349). So a narrowed apply prints `--applied
    <those tags>`, and the clear keeps whatever the row has gained since.

    A `k8s_unapplied` line needs a hand clear even though the marker discharges itself off the
    release record: a role whose change was reverted rather than deployed never gets a record
    naming it, and a line nobody can drop is a banner entry forever.

    Args:
        cls: the ledger class — `manual_plane`, `k8s_deferred` or `k8s_unapplied`. A string,
            not `gitops_ledger`'s constant, because `gitops_ledger` imports this module.
        subject: the setup role under the `--tags` value that selects it, or the k8s service.
            A generic remediation passes the placeholder `<role>` or `<service>`.
        tags: the tags the apply command names, `manual_plane` only. Empty, or just the role
            tag, is a whole-role apply and gets the bare form.
    """
    if tags and cls != "manual_plane":
        # `gitops_state.py` refuses `--applied` for any other class, so printing it would
        # hand the operator a command that fails.
        raise ValueError(f"--applied applies to manual_plane only, not {cls}")
    cmd = f"{OWED_CLEAR_CMD} {cls} {subject}"
    applied = [t for t in sorted(frozenset(tags)) if t != subject]
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
    """One pending role of the `owed` ledger's `manual_plane` class.

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
    """One pending k8s entry of the `owed` ledger: a `k8s_deferred` or `k8s_unapplied` line.

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
