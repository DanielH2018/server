# ansible/roles/setup/gitops_deploy/files/deploy_state.py
"""The deployer's state directory: the marker files under /var/lib/gitops-deploy.

Each marker is a file recording what this host believes. `gitops_markers.MARKERS` is that
files — the directory literal, every basename and the line parsers live there, copied into
every other tree that reads them; this module holds the reading and the writing.
This is a leaf: `gitops_markers`, `deploy_config` for `log`, `deploy_git` for the two pure
hold-marker decisions `clear_broad_hold` makes, `deploy_state_k8s` for the `k8s_deferred` and
`k8s_unapplied` families, `deploy_state_alerts` for the alert dedupe slots, `host_lib` and the
standard library. Nothing
else from this role, and nothing that reaches a process — a hold is written to a file, and
who decides to write one is the caller's business. Callers reach these names qualified —
`deploy_state.DeployerState(...)`. `deploy_io` re-exports them for the suite, which reads them
through the module it has always read.

Stdlib only: the unit runs under `uv run --no-project`, never from a venv. The `# DECIDED:`
marker at `templates/gitops-deploy.service.j2`'s `ExecStart` says why.
"""

import json
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
from deploy_state_alerts import AlertSlotMarkers
from deploy_state_k8s import K8sLineMarkers
from gitops_markers import (  # noqa: F401 — NO_PLAYBOOK and the entries are re-exported
    MARKERS,
    NARROWED_TO_ROLE,
    NO_PLAYBOOK,
    STATE_DIR,
    ContentionEntry,
    ManualPlaneEntry,
    parse_contention,
    parse_manual_plane,
    parse_manual_plane_tags,
)
from gitops_ledger import (
    OWED_MANUAL_PLANE,
    RECEIPT_KEEP,
    drop_owed,
    merge_manual_plane,
    parse_receipts,
    put_manual_plane,
    receipt_line,
)
from host_lib import atomic_write


class DeployerState(AlertSlotMarkers, K8sLineMarkers):
    """The marker files under /var/lib/gitops-deploy, as one object with typed accessors.

    The two k8s marker families come from `deploy_state_k8s.K8sLineMarkers` and the alert
    dedupe slots from `deploy_state_alerts.AlertSlotMarkers`, so `state.record_k8s_unapplied(...)`
    and `state.alerted_sha(...)` are reached here as they always were. This class stays the one
    place a marker file is read or written.

    The files record what this host believes — the held SHA, the plane that failed, how long
    it has been behind origin, the setup roles no tick can apply, one keyed file holding every
    alert channel's dedupe SHA and the undelivered-alert queue.
    `tests/test_deployer_state.py` pins every marker by name against `MARKERS`.

    Attributes:
        directory: where the markers live. `/var/lib/gitops-deploy` on a host; a tmp_path
            under test.
    """

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
    # off the same mount) get a named property; the alert dedupe slots are reached through
    # `alerted_sha`/`record_alerted` by the alert code that owns them.
    @property
    def hold_sha(self) -> str | None:
        """The commit this host refuses to redeploy, or None."""
        return self.read("hold")

    @property
    def hold_plane(self) -> str | None:
        """Each failed apply's playbook (and tags), `; `-joined (`hold_plane_with`), or None."""
        return self.read("hold_plane")

    # ── the per-SHA tick receipt (#3391) ──────────────────────────────────────────────────

    def record_receipt(
        self,
        origin: str,
        base: str,
        applied: dict[str, list[str]] | None = None,
        manual: dict[str, frozenset[str] | None] | None = None,
    ) -> None:
        """Merge what this tick did at `origin` into that SHA's receipt, creating it if absent.

        Args:
            origin: the SHA the tick crossed to.
            base: the commit the checkout stood on before the tick.
            applied: playbook -> the tags it applied with. Called once per plan, so a
                setup-then-deploy range ends with both.
            manual: setup role tag -> the narrowest tags its change in this range needs, or
                None where no derivation could narrow it.

        Every key the stored receipt carries beyond these survives the merge, for the reason
        `gitops_markers.parse_owed` gives. The marker keeps the newest `RECEIPT_KEEP` lines.
        """
        lines, obj = [], None
        for line in (self.read("receipts") or "").splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("origin") == origin:
                obj = parsed
            else:
                lines.append(line)
        obj = obj or {"origin": origin, "base": base}
        for key, new in (("applied", applied or {}), ("manual", manual or {})):
            held = obj.get(key) if isinstance(obj.get(key), dict) else {}
            held.update({k: sorted(v or ()) for k, v in new.items()})
            obj[key] = held
        lines.append(json.dumps(obj, sort_keys=True))
        self.write("receipts", "\n".join(lines[-RECEIPT_KEEP:]))

    def drop_receipt(self, origin: str) -> None:
        """Remove `origin`'s receipt, for a tick whose ff-merge was undone.

        The next tick re-crosses the range and writes it again, so a receipt the reset leaves
        behind would describe an apply at a SHA no tree here carries (#2382).
        """
        kept = [
            receipt_line(r)
            for r in parse_receipts(self.read("receipts"))
            if r.origin != origin
        ]
        self.write("receipts", "\n".join(kept) or None)

    # ── the setup roles this deployer cannot apply itself ─────────────────────────────────
    #
    # Written to the `owed` ledger's `manual_plane` class (#3392), one line per role carrying
    # its `playbook` and `tags` keys. The `manual_plane` line marker and its
    # `manual_plane_tags` sidecar are only READ now: a host still holding lines a deployer from
    # before the move wrote keeps them pending, and the first write here folds them into the
    # ledger (`_fold_manual_plane_lines`).

    def _manual_plane(self) -> tuple[list[ManualPlaneEntry], dict[str, frozenset[str]]]:
        """Every pending role and its tags, the ledger merged with the legacy line marker."""
        return merge_manual_plane(
            parse_manual_plane(self.read("manual_plane")),
            parse_manual_plane_tags(self.read("manual_plane_tags")),
            self.read("owed"),
        )

    def manual_plane_pending(self) -> list[ManualPlaneEntry]:
        """Every pending role, oldest first; a garbled line is skipped, never lost.

        The ledger's writers carry such a line through untouched, and
        `_fold_manual_plane_lines` leaves a garbled legacy line in its file.
        """
        return self._manual_plane()[0]

    def _fold_manual_plane_lines(self) -> None:
        """Move every readable legacy `manual_plane` line into the ledger, with its tags.

        Each role takes the merged answer `merge_manual_plane` gives, so a role in both
        sources keeps its oldest stamp. The sidecar goes whole: a row means something only
        beside its line. A line the legacy parser cannot read stays in its file, for the
        reason `owed_line_key` keeps a torn ledger line.
        """
        legacy = self.read("manual_plane")
        if legacy is None and self.read("manual_plane_tags") is None:
            return
        entries, tags = self._manual_plane()
        owed = self.read("owed")
        for entry in entries:
            owed = put_manual_plane(owed, entry, tags[entry.role])
        self.write("owed", owed or None)
        torn = [
            line for line in (legacy or "").splitlines() if not parse_manual_plane(line)
        ]
        self.write("manual_plane", "\n".join(torn) or None)
        self.write("manual_plane_tags", None)

    def _put_manual_plane_tags(self, role: str, tags: frozenset[str]) -> None:
        """Set a pending role's tags. A role that is not pending is left alone."""
        self._fold_manual_plane_lines()
        entry = next((e for e in self.manual_plane_pending() if e.role == role), None)
        if entry is not None:
            self.write("owed", put_manual_plane(self.read("owed"), entry, tags))

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

        A new line names the whole role until `record_manual_plane_tags` narrows it, so a
        reader between the two calls prints the blunt answer rather than a narrow one.

        The stamp is NOT refreshed for a role already listed. It measures how long the role
        has waited for a hand-applied run, and a later commit touching the same role is not
        that run — it is more of the same waiting. Refreshing on one would restart the clock
        every tick a push landed, and monitor-bridge could never page. (This is the opposite
        of `behind_marker`, which re-stamps on every fast-forward, because progress is
        exactly what a tick that moves the tree HAS made.)
        """
        self._fold_manual_plane_lines()
        if any(e.role == role for e in self.manual_plane_pending()):
            return False
        entry = ManualPlaneEntry(origin, playbook, role, now)
        self.write("owed", put_manual_plane(self.read("owed"), entry, frozenset()))
        return True

    def clear_manual_plane(self, role: str) -> bool:
        """Drop `role`'s line, tags and all, removing the ledger when it empties.

        Returns:
            True when a line went, False when that role was not pending — which is what an
            operator clearing twice, or naming a role nobody recorded, must get.
        """
        self._fold_manual_plane_lines()
        text, dropped = drop_owed(self.read("owed"), OWED_MANUAL_PLANE, [role])
        if not dropped:
            return False
        self.write("owed", text or None)
        return True

    def manual_plane_tags_pending(self) -> dict[str, frozenset[str]]:
        """The narrowest tags each pending role needs, by role; empty means "use the role tag"."""
        return self._manual_plane()[1]

    def record_manual_plane_tags(
        self, role: str, tags: frozenset[str] | None, line_predates: bool
    ) -> None:
        """Record the narrowest tags `role`'s pending change needs, widening on doubt.

        Args:
            role: the role, under the `--tags` value that selects it — the same key
                `record_manual_plane` writes.
            tags: what the derivation returned, or None when it refused.
            line_predates: the role's line was already there before this range recorded it,
                so an earlier range made it pending.

        Two ranges can make one role pending, because `record_manual_plane` keeps the first
        line and its first-seen stamp. The tags then UNION: both changes are merged and
        unapplied, so both tags have to run. A refusal on either side absorbs the pair — a
        range nothing could narrow needs the whole role, and a narrow tag beside it would
        under-describe the work while reading like the complete answer.

        An earlier line with EMPTY tags is the same refusal. Either an earlier derivation
        refused, or the line came from the legacy marker with no sidecar row, whose needs are
        unknown. Unknown joined with anything is the whole role.
        """
        earlier = self.manual_plane_tags_pending().get(role, frozenset())
        if tags is None or (line_predates and not earlier):
            new = frozenset()
        elif line_predates:
            new = earlier | tags
        else:
            new = tags
        self._put_manual_plane_tags(role, new)

    def restore_manual_plane_tags(self, role: str, tags: frozenset[str] | None) -> None:
        """Put `role`'s tags back to a value a caller snapshotted.

        Args:
            role: the role, under the `--tags` value that selects it.
            tags: the tags as they stood before, or None when the role had none recorded,
                which reads as the whole role.

        The inverse of `record_manual_plane_tags` for a tick whose ff-merge was undone, and it
        restores rather than subtracting because the union is not invertible: a refusal
        collapses the tags to the empty set, which no subtraction can unwind back to the
        earlier range's tags. `deploy_defer.record` takes the snapshot, `unrecord` hands it
        back (#2320).
        """
        self._put_manual_plane_tags(role, tags or frozenset())

    def clear_manual_plane_tags_applied(
        self, role: str, applied: frozenset[str]
    ) -> frozenset[str] | None:
        """Drop `applied` from `role`'s tags, keeping the line when work is left on it.

        Args:
            role: the role, under the `--tags` value that selects it.
            applied: the tags the operator actually ran. Naming `role` itself is a whole-role
                apply, and clears the line however its tags have grown.

        Returns:
            The tags still pending, or None when the whole line went — which is also what a
            role that was not pending returns. `{role}` alone means the line was KEPT because
            its tags are empty: the role needs its whole-role tag, which no narrowed apply
            covers.

        The operator's narrowed clear (#2349). `land.sh` prints `--tags kubeconfig` and the
        clear beside it, and between the two a second range can widen the tags to
        `coredns,kubeconfig`. A whole-line clear there drops `coredns` with it, leaving that
        change merged, unapplied and recorded nowhere. Only tags the apply covered entirely
        take the line.

        EMPTY tags keep the line too, and write nothing. Every printer names `--applied` only
        while it holds non-empty tags, so meeting empty ones means they changed after the
        command was printed: a later range's derivation refused, collapsing them to "the
        whole role". Clearing there drops that range unapplied.

        `clear_manual_plane` is deliberately left alone: `deploy_defer.unrecord` and
        `clear_manual_plane_applied` both depend on it dropping the line, tags and all.
        """
        if role in applied:
            self.clear_manual_plane(role)
            return None
        row = self.manual_plane_tags_pending().get(role)
        if row is None:
            return None
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

        A k8s deploy is `ansible/deploy.yml --tags <services>`, so it drops a held
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
