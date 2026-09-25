# ansible/roles/setup/gitops_deploy/files/deploy_state.py
"""The deployer's state directory: the marker files under /var/lib/gitops-deploy.

file that records what this host believes. `gitops_markers.MARKERS` is the one table of those
files — the directory literal, every basename and the line parsers live there, copied into
every other tree that reads them; this module holds the reading and the writing.
This is a leaf: `gitops_markers`, `deploy_config` for `log`, `deploy_git` for the two pure
hold-marker decisions `clear_broad_hold` makes, `host_lib` and the standard library. Nothing
else from this role, and nothing that reaches a process — a hold is written to a file, and
who decides to write one is the caller's business. Callers reach these names qualified —
`deploy_state.DeployerState(...)`. `deploy_io` re-exports them for the suite, which reads them
through the module it has always read.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

import os
import pathlib
from typing import ClassVar

from deploy_config import log
from deploy_git import (
    HOLD_PLANE_SEP,
    behind_marker,
    broad_hold_cleared_by,
    hold_plane_entries,
    hold_plane_marker,
    hold_plane_with,
)
from gitops_markers import (  # noqa: F401 — NO_PLAYBOOK and the entries are re-exported
    MARKERS,
    NARROWED_TO_ROLE,
    NO_PLAYBOOK,
    STATE_DIR,
    ContentionEntry,
    K8sDeferredEntry,
    ManualPlaneEntry,
    format_manual_plane_tags,
    parse_contention,
    parse_k8s_deferred,
    parse_manual_plane,
    parse_manual_plane_tags,
)
from host_lib import atomic_write


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
    # Marker name -> basename on disk. `gitops_markers.MARKERS` is the table; it stays
    # reachable here as `DeployerState.MARKERS` because that is the name the suite and the
    # census in `tests/test_deployer_state.py` read.
    MARKERS: ClassVar[dict[str, str]] = MARKERS

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
        """Each failed apply's playbook (and tags), `; `-joined (`hold_plane_with`), or None."""
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

    def restore_broad_applied(self, marker: str | None) -> None:
        """Put the marker back to a value a caller snapshotted, or remove it.

        Args:
            marker: the whole marker as it stood before this tick, or None when there was
                none at all.

        The reverse of `record_broad_applied` for a tick whose ff-merge was undone. One plan's
        apply is recorded inside the loop, and a LATER plan's busy lock resets the tree to
        `local` — the marker then claims a SHA this tree does not carry was applied, and
        `land.sh` reads it to tell a plane the tick applied from one it merely fast-forwarded
        past (#2382). It RESTORES rather than clearing, because an earlier tick's marker is
        still true; the single marker records the last plane applied, not a row per plan, so
        the earlier value is the only way back (the problem `Recorded.tags_before` solves for
        the `manual_plane` sidecar).
        """
        self.write("broad_applied", marker)

    # ── the setup roles this deployer cannot apply itself ─────────────────────────────────

    @property
    def manual_plane(self) -> str | None:
        """The raw `manual_plane` marker, or None when no role is pending."""
        return self.read("manual_plane")

    def manual_plane_pending(self) -> list[ManualPlaneEntry]:
        """Every pending role, oldest line first; a garbled line is skipped, never lost.

        `record_manual_plane` and `clear_manual_plane` carry such a line through untouched.
        """
        return parse_manual_plane(self.manual_plane)

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
        """Drop `role`'s line and its narrow-tag row, removing each marker when it empties.

        Returns:
            True when a line went, False when that role was not pending — which is what an
            operator clearing twice, or naming a role nobody recorded, must get.

        The sidecar row goes with the line whatever the answer. A row outliving its line is
        a row nothing can clear: every reader looks the role up by the `manual_plane` line
        it no longer has, so the stale narrowing would be handed to the NEXT range that
        records the same role, naming a tag that range never touched.
        """
        self._drop_manual_plane_tags(role)
        lines = (self.manual_plane or "").splitlines()
        kept = [line for line in lines if self._line_role(line) != role]
        if len(kept) == len(lines):
            return False
        self.write("manual_plane", "\n".join(kept) or None)
        return True

    def manual_plane_tags_pending(self) -> dict[str, frozenset[str]]:
        """The narrowest tags each pending role needs, by role; empty means "use the role tag"."""
        return parse_manual_plane_tags(self.read("manual_plane_tags"))

    def record_manual_plane_tags(
        self, role: str, tags: frozenset[str] | None, line_predates: bool
    ) -> None:
        """Record the narrowest tags `role`'s pending change needs, widening on doubt.

        Args:
            role: the role, under the `--tags` value that selects it — the same key
                `record_manual_plane` writes, so a reader joins the two by one name.
            tags: what the derivation returned, or None when it refused.
            line_predates: the role's `manual_plane` line was already there before this
                range recorded it, so an earlier range made it pending.

        Two ranges can make one role pending, because `record_manual_plane` keeps the first
        line and its first-seen stamp. The tags then UNION: both changes are merged and
        unapplied, so both tags have to run. A refusal on either side absorbs the pair — a
        range nothing could narrow needs the whole role, and a narrow tag beside it would
        under-describe the work while reading like the complete answer.

        A line that predates this range with NO row is the same refusal. Its earlier range's
        needs are unknown: the line was written by a deployer from before this sidecar, or
        its row was too garbled for `parse_manual_plane_tags`. Unknown joined with anything
        is the whole role.
        """
        pending = self.manual_plane_tags_pending()
        known = role in pending
        unknown_earlier = line_predates and not known
        if tags is None or unknown_earlier or (known and not pending[role]):
            pending[role] = frozenset()
        else:
            pending[role] = pending.get(role, frozenset()) | tags
        self.write("manual_plane_tags", format_manual_plane_tags(pending))

    def restore_manual_plane_tags(self, role: str, tags: frozenset[str] | None) -> None:
        """Put `role`'s narrow-tag row back to a value a caller snapshotted, or remove it.

        Args:
            role: the role, under the `--tags` value that selects it.
            tags: the row as it stood before, or None when the role had no row at all.

        The inverse of `record_manual_plane_tags` for a tick whose ff-merge was undone, and it
        restores rather than subtracting because the union is not invertible: a refusal
        collapses the row to the empty set, which no subtraction can unwind back to the
        earlier range's tags. `deploy_defer.record` takes the snapshot, `unrecord` hands it
        back (#2320).
        """
        pending = self.manual_plane_tags_pending()
        if tags is None:
            pending.pop(role, None)
        else:
            pending[role] = tags
        self.write("manual_plane_tags", format_manual_plane_tags(pending))

    def _drop_manual_plane_tags(self, role: str) -> None:
        """Drop one role's narrow-tag row, removing the marker when it was the last one."""
        pending = self.manual_plane_tags_pending()
        if pending.pop(role, None) is None:
            return
        self.write("manual_plane_tags", format_manual_plane_tags(pending))

    def clear_manual_plane_tags_applied(
        self, role: str, applied: frozenset[str]
    ) -> frozenset[str] | None:
        """Drop `applied` from `role`'s row, keeping the line when work is left on it.

        Args:
            role: the role, under the `--tags` value that selects it.
            applied: the tags the operator actually ran. Naming `role` itself is a whole-role
                apply, and clears the line however the row has grown.

        Returns:
            The tags still pending, or None when the whole line went — which is also what a
            role that was not pending returns. `{role}` alone means the line was KEPT because
            the row is empty or missing: the role needs its whole-role tag, which no narrowed
            apply covers.

        The operator's narrowed clear (#2349). `land.sh` prints `--tags kubeconfig` and the
        clear beside it, and between the two a second range can widen the row to
        `coredns,kubeconfig`. A whole-line clear there drops `coredns` with it, leaving that
        change merged, unapplied and recorded nowhere. Only a row the apply covered entirely
        takes the line.

        An EMPTY or MISSING row keeps the line too, and writes nothing. Every printer names
        `--applied` only while it holds a non-empty row, so meeting an empty one means the row
        changed after the command was printed: a later range's derivation refused, collapsing
        the row to "the whole role". Clearing there drops that range unapplied. A missing row
        is the same unknown — a line written before the sidecar existed, or a row too garbled
        to parse.

        `clear_manual_plane` is deliberately left alone: `deploy_defer.unrecord` and
        `clear_manual_plane_applied` both depend on it dropping the line and the row together.
        """
        if role in applied:
            self.clear_manual_plane(role)
            return None
        row = self.manual_plane_tags_pending().get(role)
        if not row:
            return frozenset({role})
        # DECIDED: subtract-and-keep, with no guard on the origin SHA the command was printed
        # for. A second range that needs a tag ALREADY in the row (PR-B also needs
        # `kubeconfig`) leaves the row unchanged, so an operator who applied `kubeconfig`
        # before PR-B fast-forwarded clears PR-B's pending work with their own. Closing that
        # needs the printed command to carry a token of what the row was, which widens the
        # marker grammar the sidecar exists to leave alone. Accepted in #2349 (PR #2364).
        remaining = row - applied
        if not remaining:
            self.clear_manual_plane(role)
            return None
        self.restore_manual_plane_tags(role, remaining)
        return remaining

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

    # ── the promoted bumps a broad tick deferred for lack of budget ───────────────────────

    def _k8s_line_pending(self, marker: str) -> list[K8sDeferredEntry]:
        """Every line `marker` still holds, oldest first.

        `k8s_deferred` and `k8s_unapplied` share this format and these three helpers; what
        differs is who reads them, which is the marker table's business.
        """
        return parse_k8s_deferred(self.read(marker))

    def _record_k8s_line(
        self, marker: str, origin: str, services, now: float
    ) -> list[str]:
        """Append a line per service `marker` does not list yet. Returns the ones added."""
        lines: list[str] = (self.read(marker) or "").splitlines()
        listed = {e.service for e in self._k8s_line_pending(marker)}
        added = sorted(set(services) - listed)
        lines.extend(f"{origin} {service} {now:.0f}" for service in added)
        if added:
            self.write(marker, "\n".join(lines))
        return added

    def _clear_k8s_lines(self, marker: str, services) -> list[str]:
        """Drop `marker`'s lines naming any of `services`. Returns the names cleared.

        A line this cannot parse is carried through untouched, the way `clear_manual_plane`
        carries one: a torn line is skipped by every reader, and dropping it here would lose
        the only record that something was deferred.
        """
        wanted = set(services)
        kept, cleared = [], []
        for line in (self.read(marker) or "").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] in wanted:
                cleared.append(parts[1])
                continue
            kept.append(line)
        if cleared:
            self.write(marker, "\n".join(kept) or None)
        return sorted(set(cleared))

    def k8s_deferred_pending(self) -> list[K8sDeferredEntry]:
        """Every bump the `k8s_deferred` marker still holds, oldest line first."""
        return self._k8s_line_pending("k8s_deferred")

    def record_k8s_deferred(self, origin: str, services, now: float) -> list[str]:
        """Record the bumps this tick merged and could not deploy. Returns the ones added.

        A service already listed keeps its first-seen stamp, which is the age monitor-bridge
        pages on, exactly as `record_manual_plane` keeps a role's. That stamp is what makes
        the marker safe to page on: a bump deferred ten minutes ago is a tick that ran out of
        wall clock, not a fault.

        Args:
            origin: the origin SHA the tick merged. The bump is live in the tree at that
                commit, so an operator's deploy applies exactly it.
            services: the deferred service tags.
            now: the first-seen stamp, in `time.time()` terms.
        """
        return self._record_k8s_line("k8s_deferred", origin, services, now)

    def clear_k8s_deferred(self, services) -> list[str]:
        """Drop the `k8s_deferred` lines naming any of `services`. Returns the names cleared."""
        return self._clear_k8s_lines("k8s_deferred", services)

    # ── the k8s changes a tick merged and will never apply (#2570) ────────────────────────
    # The non-paging half of the defer-and-alert channel: the SessionStart banner and the
    # deployer's journal read `k8s_unapplied` and nothing else does. `origin` is the SHA the
    # tick merged, which `deploy_defer.discharge_k8s_unapplied` compares a release record to.

    def k8s_unapplied_pending(self) -> list[K8sDeferredEntry]:
        """Every k8s role change the `k8s_unapplied` marker still holds, oldest line first."""
        return self._k8s_line_pending("k8s_unapplied")

    def record_k8s_unapplied(self, origin: str, services, now: float) -> list[str]:
        """Record the k8s role changes this tick merged and will never apply. Returns the added."""
        return self._record_k8s_line("k8s_unapplied", origin, services, now)

    def clear_k8s_unapplied(self, services) -> list[str]:
        """Drop the `k8s_unapplied` lines naming any of `services`. Returns the names cleared."""
        return self._clear_k8s_lines("k8s_unapplied", services)

    # ── consecutive ticks deferred on a busy service lock ─────────────────────────────────

    def contention_pending(self) -> ContentionEntry | None:
        """The streak the `contention_since` marker records, or None for a garbled marker.

        `record_contention` overwrites a garbled marker rather than carrying it.
        """
        return parse_contention(self.read("contention"))

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

    def hold_failed_apply(self, sha: str, playbook: str, tags: list[str]) -> None:
        """Hold `sha` for a failed apply of `playbook`/`tags`, beside any plane already held.

        Added to `hold_plane`, never written over it: see `deploy_git.hold_plane_with`.
        """
        self.write_hold(sha)
        self.write("hold_plane", hold_plane_with(self.hold_plane, playbook, tags))

    def clear_broad_hold(self, playbook: str, tags: list[str]) -> None:
        """Clear the hold after a broad apply, but only once no held plane is left unapplied.

        A hold says a plane is unapplied, and every consumer gates on `hold_sha` — so
        clearing it after a success in a DIFFERENT plane turns GitOps Deploy — Status green
        over a plane nothing has applied (issue #878). This apply drops the entries it
        covers; while one survives, the tick still succeeded and the marker is kept.
        """
        held = hold_plane_entries(self.hold_plane)
        left = [e for e in held if not broad_hold_cleared_by(e, playbook, tags)]
        if left:
            if left != held:
                self.write("hold_plane", HOLD_PLANE_SEP.join(left))
            log(
                f"hold kept: {HOLD_PLANE_SEP.join(left)} is still unapplied "
                f"(this tick applied {hold_plane_marker(playbook, tags)})"
            )
            return
        self.write("hold_plane", None)
        self.write_hold(None)

    def clear_service_hold(self, services: set[str]) -> None:
        """Clear a hold after a successful service deploy, unless it leaves a plane unapplied.

        A k8s or Docker deploy is `ansible/deploy.yml --tags <services>`, so it drops a held
        entry naming that playbook at a subset of those tags — a failed bump on a broad tick
        writes exactly that, and the fix-forward deploy of the same service is its way out.
        Any other entry stays held: without this, an unrelated service deploy clears
        `hold_sha` and orphans `hold_plane`, which `gitops_status` never reads on its own.
        """
        if self.hold_plane and not services:
            log(f"hold kept: {self.hold_plane} is still unapplied")
            return
        self.clear_broad_hold("ansible/deploy.yml", sorted(services))

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
