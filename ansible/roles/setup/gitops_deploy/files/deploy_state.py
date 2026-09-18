# ansible/roles/setup/gitops_deploy/files/deploy_state.py
"""The deployer's state directory: the marker files under /var/lib/gitops-deploy.

`DeployerState` is the whole of it — one object with a typed accessor per marker, over every
file that records what this host believes. `MARKERS` is the one table of those files — the
directory literal and every basename live here and nowhere else in the role; this module holds
the reading and the writing.

This is a leaf: `deploy_config` for `log`, `deploy_git` for the two pure hold-marker decisions
`clear_broad_hold` makes, `host_lib` and the standard library. Nothing else from this role,
and nothing that reaches a process — a hold is written to a file, and who decides to write one
is the caller's business. Callers reach these names qualified —
`deploy_state.DeployerState(...)`. `deploy_io` re-exports them for the suite, which reads them
through the module it has always read.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

import os
import pathlib
from typing import ClassVar, NamedTuple

from deploy_config import log
from deploy_git import behind_marker, broad_hold_cleared_by, hold_plane_marker
from host_lib import atomic_write


STATE_DIR = "/var/lib/gitops-deploy"

# What the playbook field holds for a role no playbook applies (`common`).
NO_PLAYBOOK = "none"


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


class ContentionEntry(NamedTuple):
    """The `contention_since` marker: consecutive ticks deferred on a busy service lock.

    Attributes:
        origin: the origin SHA the most recent deferred tick was trying to reach.
        lock: the lock that stayed busy, as `deploy_locks.ServiceLockBusy.lock` names it.
        first_seen: when the first tick of the streak deferred, in `time.time()` terms. The
            age monitor-bridge and the SessionStart banner read; never refreshed within a
            streak, for the reason `record_manual_plane` gives.
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


class DeployerState:
    """The marker files under /var/lib/gitops-deploy, as one object with typed accessors.

    The files record what this host believes — the held SHA, the plane that failed, how long
    it has been behind origin, the setup roles no tick can apply, one dedupe marker per alert
    channel, the undelivered-alert queue, the staging tick ledger and the operator's staging
    override — and they were reached through one module constant each plus a pair of bare
    `_read_marker`/`_write_marker` helpers, so nothing described the state as a whole. This
    is that description. The paths, the file contents and the empty-vs-missing semantics are
    unchanged, and `tests/test_deployer_state.py` pins every marker by name against `MARKERS`.

    Attributes:
        directory: where the markers live. `/var/lib/gitops-deploy` on a host; a tmp_path
            under test.
    """

    # Attribute name -> basename on disk. THE table of what lives in the state directory: the
    # deployer reads and writes every marker through it, the `state_dir` fixture repoints the
    # whole set by replacing one instance, and `tests/test_deployer_state.py` pins every pair
    # by name. What each file records, and why it exists, is beside its entry.
    MARKERS: ClassVar[dict[str, str]] = {
        # The SHA whose deploy failed its health gate or broad apply; the host is HELD there
        # until an operator clears it (`write_hold`, `clear_broad_hold`, `clear_service_hold`).
        "hold": "hold_sha",
        # The playbook (and tags) whose broad apply failed, written beside `hold_sha`. That
        # marker alone is service-shaped — monitor-bridge's message says "revert the offending
        # PR", the wrong remediation for a broad apply: the tree is already fast-forwarded and
        # a playbook is what broke, so reverting the PR undoes nothing. This names what to
        # re-run instead.
        "hold_plane": "hold_plane",
        # The last broad plane this host APPLIED, as `<origin_sha> <playbook> <tags>`. The
        # only durable evidence that a tick applied a plane, as against fast-forwarding past
        # it: `behind_since` empty says local == origin, which any session's `git merge
        # --ff-only` also produces, and after that `next_action()` returns `noop` forever so
        # the plane is stranded (issue #1537). Read by `land.sh` before it says `settled`.
        "broad_applied": "broad_applied",
        # One line per setup role this host fast-forwarded past and cannot apply itself,
        # `"<origin_sha> <playbook-or-none> <role> <unix_ts>"`. See `record_manual_plane`.
        "manual_plane": "manual_plane",
        # `"<origin_sha> <lock> <unix_ts_first_seen> <unix_ts_last_seen> <count>"` while
        # consecutive ticks defer on one busy service lock. See `record_contention`.
        "contention": "contention_since",
        # The unix time the last tick completed; monitor-bridge's GitOps Alive reads its age.
        "last_run": "last_run",
        # Origin SHA recorded while local and origin have DIVERGED (`deploy_logic.is_diverged`):
        # the deployer can't fast-forward and noops forever, so origin's new commits never
        # deploy while both GitOps monitors stay green. monitor-bridge reads this off the same
        # :ro mount as `hold_sha` and pages GitOps Status until the host tree is reconciled.
        "diverged": "diverged_sha",
        # `"<origin_sha> <unix_ts_first_seen>"` while the host is BEHIND origin at the end of
        # a tick — origin strictly ahead and we did not converge. Every reason lands here: a
        # deferred broad change, a long-dirty tree, a hold. The broad path in particular is
        # invisible otherwise — it never ff-merges, so the host parks behind master
        # indefinitely while `last_run` keeps ticking (Alive green) and `is_diverged` stays
        # false (origin is a strict descendant, so Status green too). That is how daniel-server
        # sat on a 12-commit-old tree for hours on 2026-08-02 with every GitOps signal green,
        # until the un-deployed Pi-hole DNS records were noticed by hand.
        #
        # The timestamp is what makes this safe to page on: a normal push is behind for one
        # tick, and an operator mid-edit (the dirty path, deliberately treated as healthy) is
        # behind for as long as they are editing. Only sustained behind-ness is a problem, so
        # monitor-bridge applies an age threshold. The first-seen stamp is preserved across
        # ticks and reset ONLY on convergence — not per-SHA, or a steady trickle of pushes to
        # a permanently-stuck host would keep restarting the clock. See `record_behind`.
        "behind": "behind_since",
        # The sorted stale-compose set last alerted on, so a lingering stale dir doesn't
        # re-page every tick — only a CHANGED set (new stale dir, or one cleaned up) re-alerts.
        "stale_composes": "stale_composes_alerted",
        # Per-SHA dedupe markers, one per alert channel: the operator is paged ONCE per origin
        # SHA about a deferred broad change, a secrets-only push (a rotated value with no
        # service template change), a tasks-only push (a role tasks/ change, not
        # auto-deployed), a meta-only push (a role meta/deps.yml change — the cross-service
        # deploy graph), a k8s-role push (no mechanism here ever applies one, so there is no
        # "rode a redeploy" case to dedupe against `deployed`), a stale denylist (the DISARM
        # itself is stateless and recomputed every tick — only the page is throttled), a
        # master tip that FAILED CI (until the operator fixes or reverts; there is no marker
        # for `ci_pending`, which resolves itself within a tick or two and stays silent), and
        # a staging-gate verdict — rather than every tick for as long as the state persists.
        "broad_alerted": "broad_alerted_sha",
        "secrets_alerted": "secrets_alerted_sha",
        "tasks_alerted": "tasks_alerted_sha",
        "meta_alerted": "meta_alerted_sha",
        "k8s_alerted": "k8s_alerted_sha",
        "stale_denylist_alerted": "stale_denylist_alerted_sha",
        # The checkout SHA the denylist reconcile last ran against — the once-per-SHA guard on
        # `deploy_phases.reconcile_denylist`. It bounds BOTH directions: the git read is
        # skipped entirely while the checkout has not moved, and a mismatch a re-render cannot
        # fix (a config rendered from an unpushed tree) re-renders once per SHA rather than
        # every tick.
        "denylist_rendered": "denylist_rendered_sha",
        "ci_alerted": "ci_alerted_sha",
        "staging_alerted": "staging_alerted_sha",
        # The last dirty-alert slot (`YYYY-MM-DD:am|pm`) paged for a dirty working tree. The
        # tick runs every 30 min, so without this an open edit session would re-alert all day;
        # one alert per slot — a morning slot at/after DIRTY_ALERT_MORNING_HOUR (08:00 CT) and
        # an evening slot at/after DIRTY_ALERT_EVENING_HOUR (20:00 CT). See
        # `deploy_logic.dirty_alert_slot`.
        "dirty_alerted": "dirty_alerted_date",
        # The three that are not per-SHA dedupe markers. They are here for the same reason as
        # the rest — so a caller names a marker rather than carrying a path — and because the
        # `state_dir` fixture repoints the whole object at once, which a path threaded through
        # a function argument would escape. `deploy_alerts`, `deploy_staging` and
        # `deploy_handlers` reach them through `state.path(...)`.
        #
        # Undelivered post-merge alerts, retried at the TOP of every tick. The
        # secrets/tasks/meta/combined channels `git merge --ff-only` BEFORE their
        # delivery-gated marker write, so once merged local==origin and the next tick
        # short-circuits at `noop` (main) before ever re-reaching the alert code — a single
        # transient discord() failure (timeout/5xx/Cloudflare-1010/DNS blip) would otherwise
        # drop that alert forever (the rotated secret sits stale in its container / the
        # tasks|meta change sits ff-merged-but-unapplied, with no other signal). This queue
        # decouples DELIVERY from the git action: an alert that fails to send is persisted
        # here keyed by "<channel>:<sha>" and `drain_pending()` resends it every tick until a
        # confirmed 2xx clears it. The per-SHA markers above still gate DETECTION (so a
        # delivered alert isn't re-queued on the broad path's every-tick re-eval); this queue
        # owns delivery.
        "pending_alerts": "pending_alerts.json",
        # Where a real gated tick's verdict is recorded. Deliberately NOT the backfill ledger:
        # that file is planned from — `backfill_staging_gate.py --since-ledger` reads its
        # newest row to build the next window — so a tick row in it would send the hourly
        # ratchet to a window it cannot run. `gitops_deploy_staging_tick_ledger` in the role's
        # defaults is the same path, tied by
        # `test_the_tick_ledger_constant_matches_the_ansible_default`.
        "staging_ticks": "staging-ticks.jsonl",
        # The operator's one-tick escape hatch, armed by creating the file and disarmed by
        # removing it. Decision 4: "Build the override before the gate. A gate with no escape
        # hatch becomes a gate somebody deletes at 2 AM, and nobody reviews the deletion."
        #
        # It is CONSUMED at the point the gate would block, never at the point it is read.
        # Consuming on entry would spend it on the first tick after arming — which is usually
        # a tick with nothing to gate — and leave the operator's actual push facing the block
        # with the hatch already gone.
        "staging_override": "staging_gate_override",
    }

    def __init__(self, directory: str | pathlib.Path = STATE_DIR) -> None:
        self.directory = str(directory)

    def path(self, marker: str) -> str:
        """The absolute path of one marker.

        Raises:
            KeyError: `marker` is not one of `MARKERS` — a typo is a mistake, not a new file.
        """
        return os.path.join(self.directory, self.MARKERS[marker])

    def read(self, marker: str) -> str | None:
        """The marker's stripped contents, or None when it is absent or empty.

        Absent and empty deliberately read the same: a marker is armed by holding a SHA and
        disarmed by being removed, and a torn write that left a zero-length file must read as
        disarmed rather than as a SHA of "".

        Raises:
            OSError: the file exists but could not be read — an unreadable state directory
                (a wrong mode, a failed mount) is NOT "no hold". Swallowing it here is how a
                held host reports converged, so it propagates and the tick pages.
        """
        try:
            with open(self.path(marker)) as fh:
                return fh.read().strip() or None
        except FileNotFoundError:
            return None

    def write(self, marker: str, value: str | None) -> None:
        """Set the marker to `value`, or remove it when `value` is None.

        The write is atomic (temp + rename, see `host_lib.atomic_write`): a torn marker is a
        hold that reads as cleared.
        """
        if value is None:
            try:
                os.remove(self.path(marker))
            except FileNotFoundError:
                pass
        else:
            atomic_write(self.path(marker), value)

    # The four markers with a reader outside this deployer (monitor-bridge reads three of them
    # off the same mount) get a named property; the per-channel dedupe markers are reached
    # through read()/write() by the alert code that owns them.
    @property
    def hold_sha(self) -> str | None:
        """The commit this host refuses to redeploy, or None."""
        return self.read("hold")

    @property
    def hold_plane(self) -> str | None:
        """The playbook (and tags) whose broad apply failed, or None."""
        return self.read("hold_plane")

    @property
    def broad_applied(self) -> str | None:
        """`"<origin_sha> <playbook> <tags>"` for the last broad plane applied, or None."""
        return self.read("broad_applied")

    def record_broad_applied(self, origin: str, playbook: str, tags: list[str]) -> None:
        """Record that this host applied `playbook`/`tags` at `origin`.

        Written only after `deploy_io.deploy_broad` returned, so the marker means "applied",
        never "attempted" — the failure path writes `hold_sha`/`hold_plane` instead.
        """
        self.write(
            "broad_applied", f"{origin} {hold_plane_marker(playbook, tags)}".strip()
        )

    # ── the setup roles this deployer cannot apply itself ─────────────────────────────────

    @property
    def manual_plane(self) -> str | None:
        """The raw `manual_plane` marker, or None when no role is pending."""
        return self.read("manual_plane")

    def manual_plane_pending(self) -> list[ManualPlaneEntry]:
        """Every pending role, oldest line first.

        A line this cannot parse is SKIPPED rather than guessed at, the way
        `checks/gitops.py::_parse_behind` treats a garbled `behind_since`: the age it would
        carry decides whether monitor-bridge pages, and a page nobody can silence on garbage
        teaches an operator to ignore the tile. `record_manual_plane` and
        `clear_manual_plane` still carry such a line through, so it is skipped, never lost.
        """
        entries = []
        for line in (self.manual_plane or "").splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            try:
                at = float(parts[3])
            except ValueError:
                continue
            entries.append(ManualPlaneEntry(parts[0], parts[1], parts[2], at))
        return entries

    def record_manual_plane(
        self, origin: str, playbook: str, role: str, now: float
    ) -> bool:
        """Record that `role` changed in `origin`'s range and no tick can apply it.

        Args:
            origin: the origin SHA the tick fast-forwarded to.
            playbook: the playbook that applies the role, or `NO_PLAYBOOK` for one no
                playbook includes.
            role: the role, under the `--tags` value that selects it.
            now: a first-seen stamp, in `time.time()` terms.

        Returns:
            True when a line was appended, False when this role was already pending.

        The stamp is NOT refreshed for a role already listed. It measures how long the role
        has waited for a hand-applied run, and a later commit touching the same role is not
        that run — it is more of the same waiting. Refreshing on one would restart the clock
        every tick a push landed, and monitor-bridge could never page. (This is the opposite
        of `behind_marker`, which re-stamps on every fast-forward, because progress is
        exactly what a tick that moves the tree HAS made.)
        """
        lines: list[str] = (self.manual_plane or "").splitlines()
        if any(self._line_role(line) == role for line in lines):
            return False
        lines.append(f"{origin} {playbook} {role} {now}")
        self.write("manual_plane", "\n".join(lines))
        return True

    def clear_manual_plane(self, role: str) -> bool:
        """Drop `role`'s line, removing the marker when it was the last one.

        Returns:
            True when a line went, False when that role was not pending — which is what an
            operator clearing twice, or naming a role nobody recorded, must get.
        """
        lines = (self.manual_plane or "").splitlines()
        kept = [line for line in lines if self._line_role(line) != role]
        if len(kept) == len(lines):
            return False
        self.write("manual_plane", "\n".join(kept) or None)
        return True

    def clear_manual_plane_applied(self, playbook: str, tags: list[str]) -> list[str]:
        """Drop the pending roles this apply covered, and return them.

        Keyed on the playbook AND the tag, never the tag alone: `initial_setup.yml --tags
        k3s` is precisely the run that exits 0 having matched no task, so treating it as an
        apply of k3s would clear the marker over a change nothing applied — the failure
        `setup_tags_for` returns an empty set to avoid.

        No role reaches this today, because every role the marker can hold is applied by a
        playbook this deployer never runs. It is the clearing half of a marker whose writer
        would otherwise have no reverse, and it is what a role promoted into
        `initial_setup.yml` needs on the day it is.
        """
        wanted = set(tags)
        cleared = [
            e.role
            for e in self.manual_plane_pending()
            if e.playbook == playbook and e.role in wanted
        ]
        for role in cleared:
            self.clear_manual_plane(role)
        return cleared

    # ── consecutive ticks deferred on a busy service lock ─────────────────────────────────

    def contention_pending(self) -> ContentionEntry | None:
        """The streak the `contention_since` marker records, or None.

        A marker this cannot parse reads as None, the way `_parse_behind` treats a garbled
        `behind_since`: its age decides whether monitor-bridge pages, and a page raised off
        garbage names no lock and cannot be cleared. `record_contention` overwrites such a
        marker rather than carrying it.
        """
        parts = (self.read("contention") or "").split()
        if len(parts) != 5:
            return None
        try:
            return ContentionEntry(
                parts[0], parts[1], float(parts[2]), float(parts[3]), int(parts[4])
            )
        except ValueError:
            return None

    def record_contention(self, origin: str, lock: str, now: float) -> ContentionEntry:
        """Record that this tick deferred on `lock`, extending the streak or starting one.

        The first-seen stamp survives across the streak and only `last_seen` and `count`
        move: the age is how long a hand-held lock has kept the tick from deploying, and each
        further tick that defers is more of the same waiting, not a fresh start. It survives
        a CHANGE of lock name too, on purpose: the streak measures "this deployer could not
        deploy", not one lock's age, and two holders wedging alternate ticks would otherwise
        reset the clock between them and never page. The marker names the latest lock.

        Args:
            origin: the origin SHA this tick was trying to reach.
            lock: the lock that stayed busy; `deploy_locks.SERVICE_LOCK_ALL` or a tag. An
                empty name is recorded as `unknown` so the marker keeps its five fields.
            now: the current time, in `time.time()` terms.

        Returns:
            The entry as written.
        """
        prior = self.contention_pending()
        entry = ContentionEntry(
            origin,
            lock or "unknown",
            prior.first_seen if prior else now,
            now,
            (prior.count if prior else 0) + 1,
        )
        self.write(
            "contention",
            f"{entry.origin} {entry.lock} {entry.first_seen} {entry.last_seen} {entry.count}",
        )
        return entry

    def clear_contention(self) -> bool:
        """Drop the marker; True when one was there. An operator's clear and the tick's."""
        if self.read("contention") is None:
            return False
        self.write("contention", None)
        return True

    def clear_contention_unless_touched_since(self, tick_started: float) -> bool:
        """Clear the streak when this tick ended some other way than a contention defer.

        `for_contention` stamps `last_seen` during the tick, so a marker whose `last_seen`
        predates `tick_started` was not written by this tick, and the lock has stopped
        wedging it — whether the tick deployed, parked, or found nothing to do. A marker
        this cannot parse is cleared too: nothing can ever extend it.

        Returns:
            True when a marker was removed.
        """
        entry = self.contention_pending()
        if entry is not None and entry.last_seen >= tick_started:
            return False
        return self.clear_contention()

    @staticmethod
    def _line_role(line: str) -> str | None:
        """The role field of one marker line, or None when the line has no third field."""
        parts = line.split()
        return parts[2] if len(parts) > 2 else None

    @property
    def diverged_sha(self) -> str | None:
        """The origin SHA recorded while local and origin have diverged, or None."""
        return self.read("diverged")

    @property
    def behind_since(self) -> str | None:
        """`"<origin_sha> <unix_ts_first_seen>"` while behind origin, or None."""
        return self.read("behind")

    # ── holding, and the two ways a hold clears ───────────────────────────────────────────

    def write_hold(self, sha: str | None) -> None:
        """Record `sha` as the commit this host refuses to redeploy, or clear the hold."""
        self.write("hold", sha)

    def clear_broad_hold(self, playbook: str, tags: list[str]) -> None:
        """Clear the hold after a broad apply, but only if this apply covered the held plane.

        A hold says one plane is unapplied, and every consumer gates on `hold_sha` — so
        clearing it after a success in a DIFFERENT plane turns GitOps Deploy — Status green
        over a plane nothing has applied (issue #878). When the hold survives, the tick still
        succeeded: the marker is the only thing kept.
        """
        held = self.hold_plane or ""
        if not broad_hold_cleared_by(held, playbook, tags):
            log(
                f"hold kept: {held} is still unapplied "
                f"(this tick applied {hold_plane_marker(playbook, tags)})"
            )
            return
        self.write("hold_plane", None)
        self.write_hold(None)

    def clear_service_hold(self) -> None:
        """Clear a hold after a successful service deploy, unless a broad plane is unapplied.

        A k8s or Docker deploy applies no plane, so it is never evidence that the plane a
        broad hold names has been applied. Without this, an unrelated service deploy clears
        `hold_sha` and orphans `hold_plane`, which `gitops_status` never reads on its own.
        """
        held = self.hold_plane
        if held:
            log(
                f"hold kept: {held} is still unapplied; a service deploy does not clear it"
            )
            return
        self.write_hold(None)

    def record_behind(
        self, origin: str, behind: bool, now: float, *, fast_forwarded: bool
    ) -> None:
        """Record whether this host ended the tick behind origin (see `behind_marker`).

        Args:
            origin: `origin/<branch>` as the tick pinned it.
            behind: whether `local` is a strict ancestor of `origin` — the caller does the
                ancestry query, because that reaches git and this object reaches only files.
            now: the current time, in `time.time()` terms, for a first-seen stamp.
            fast_forwarded: whether the tick moved the tree, which is what the stamp
                measures the absence of. The caller compares HEAD before and after, because
                that reaches git too.

        Called AFTER main() so it records the state the tick finished in, not the one it
        started in: a tick that deployed successfully converged and must clear the marker
        rather than leave a stale one for the next 30 minutes.
        """
        self.write(
            "behind",
            behind_marker(
                behind,
                origin,
                self.behind_since,
                now,
                fast_forwarded=fast_forwarded,
            ),
        )
