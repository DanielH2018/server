# ansible/roles/setup/gitops_deploy/files/deploy_state.py
"""The deployer's state directory: the marker files under /var/lib/gitops-deploy.

`DeployerState` is the whole of it — one object with a typed accessor per marker, over the
fifteen files that record what this host believes. `gitops_deploy.py` still declares the path
literals, because an Ansible default is pinned against one of them; this module holds the
reading and the writing.

This is a leaf: it imports `host_lib` and the standard library, and nothing else from this
role. Callers reach these names qualified — `deploy_state.DeployerState(...)`. `deploy_io`
re-exports them for the suite, which reads them through the module it has always read.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

import os
import pathlib
from typing import ClassVar

from host_lib import atomic_write


STATE_DIR = "/var/lib/gitops-deploy"


class DeployerState:
    """The marker files under /var/lib/gitops-deploy, as one object with typed accessors.

    Fifteen files recorded what this host believes — the held SHA, the plane that failed, how
    long it has been behind origin, and one dedupe marker per alert channel — through fifteen
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
        "ci_alerted": "ci_alerted_sha",
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
