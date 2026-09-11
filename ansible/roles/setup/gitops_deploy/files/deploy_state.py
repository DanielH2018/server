# ansible/roles/setup/gitops_deploy/files/deploy_state.py
"""The deployer's state directory: the marker files under /var/lib/gitops-deploy.

`DeployerState` is the whole of it — one object with a typed accessor per marker, over every
file that records what this host believes (`MARKERS` is the list). `gitops_deploy.py` still
declares the path literals, because an Ansible default is pinned against one of them; this
module holds the reading and the writing.

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


class DeployerState:
    """The marker files under /var/lib/gitops-deploy, as one object with typed accessors.

    The files record what this host believes — the held SHA, the plane that failed, how long
    it has been behind origin, the setup roles no tick can apply, one dedupe marker per alert
    channel, the undelivered-alert queue, the staging tick ledger and the operator's staging
    override — and they were reached through one module constant each plus a pair of bare
    `_read_marker`/`_write_marker` helpers, so nothing described the state as a whole. This
    is that description. The paths, the file contents and the empty-vs-missing semantics are
    unchanged; `gitops_deploy.py` still holds the literal
    constants because the tick ledger's Ansible default is pinned against one of them and the
    test suite repoints the rest, and `tests/test_deployer_state.py` asserts the two agree.

    Attributes:
        directory: where the markers live. `/var/lib/gitops-deploy` on a host; a tmp_path
            under test.
    """

    # Attribute name -> basename on disk. Every entry is a file `_read_marker` used to read.
    MARKERS: ClassVar[dict[str, str]] = {
        "hold": "hold_sha",
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
        "last_run": "last_run",
        "diverged": "diverged_sha",
        "behind": "behind_since",
        "stale_composes": "stale_composes_alerted",
        "broad_alerted": "broad_alerted_sha",
        "secrets_alerted": "secrets_alerted_sha",
        "tasks_alerted": "tasks_alerted_sha",
        "meta_alerted": "meta_alerted_sha",
        "k8s_alerted": "k8s_alerted_sha",
        "stale_denylist_alerted": "stale_denylist_alerted_sha",
        "denylist_rendered": "denylist_rendered_sha",
        "ci_alerted": "ci_alerted_sha",
        # The three that are not per-SHA dedupe markers. They are here for the same reason as
        # the rest — so a caller names a marker rather than carrying a path — and because the
        # `state_dir` fixture repoints the whole object at once, which a path threaded through
        # a function argument would escape. `deploy_alerts`, `deploy_staging` and
        # `deploy_handlers` reach them through `state.path(...)`.
        "pending_alerts": "pending_alerts.json",
        "staging_ticks": "staging-ticks.jsonl",
        "staging_override": "staging_gate_override",
        "staging_alerted": "staging_alerted_sha",
        "dirty_alerted": "dirty_alerted_date",
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
        `checks/service.py::_parse_behind` treats a garbled `behind_since`: the age it would
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

        The stamp is NOT refreshed for a role already listed, for the same reason
        `behind_marker` keeps its first-seen: a trickle of pushes touching the same role
        would otherwise restart the clock every tick and monitor-bridge could never page.
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
