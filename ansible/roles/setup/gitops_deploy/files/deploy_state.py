# ansible/roles/setup/gitops_deploy/files/deploy_state.py
"""The deployer's state directory: the marker files under /var/lib/gitops-deploy.

`DeployerState` is the whole of it — one object with a typed accessor per marker, over the
fifteen files that record what this host believes. `gitops_deploy.py` still declares the path
literals, because an Ansible default is pinned against one of them; this module holds the
reading and the writing.

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
from typing import ClassVar

from deploy_config import log
from deploy_git import behind_marker, broad_hold_cleared_by, hold_plane_marker
from host_lib import atomic_write


STATE_DIR = "/var/lib/gitops-deploy"


class DeployerState:
    """The marker files under /var/lib/gitops-deploy, as one object with typed accessors.

    Nineteen files record what this host believes — the held SHA, the plane that failed, how
    long it has been behind origin, one dedupe marker per alert channel, the undelivered-alert
    queue, the staging tick ledger and the operator's staging override — through nineteen
    module constants and a pair of bare `_read_marker`/`_write_marker` helpers, so nothing
    described the state as a whole. This is that description. The paths, the file contents and
    the empty-vs-missing semantics are unchanged; `gitops_deploy.py` still holds the literal
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

    def record_behind(self, origin: str, behind: bool, now: float) -> None:
        """Record whether this host ended the tick behind origin (see `behind_marker`).

        Args:
            origin: `origin/<branch>` as the tick pinned it.
            behind: whether `local` is a strict ancestor of `origin` — the caller does the
                ancestry query, because that reaches git and this object reaches only files.
            now: the current time, in `time.time()` terms, for a first-seen stamp.

        Called AFTER main() so it records the state the tick finished in, not the one it
        started in: a tick that deployed successfully converged and must clear the marker
        rather than leave a stale one for the next 30 minutes. The first-seen stamp inside
        `behind_marker` is preserved across ticks and reset only on convergence.
        """
        self.write("behind", behind_marker(behind, origin, self.behind_since, now))
