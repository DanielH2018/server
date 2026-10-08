# ansible/roles/setup/gitops_deploy/files/deploy_state_alerts.py
"""The per-SHA alert dedupe slots, as a mixin `deploy_state.DeployerState` inherits.

One `alerted` marker holds every channel as a `"<slot> <origin_sha>"` line, and
`gitops_markers.ALERT_SLOTS` is the set of slots. Seven `<channel>_alerted_sha` files held the
same state before #3047. A one-shot `migrate_alerted` folded a live host's copies in; it was
deleted in #3075, once daniel-box's first tick after #3047 had run it and removed the files.

Split out for the reason `deploy_state_k8s` was (#2663): `deploy_state.py` stood at the
600-line module cap, so the collapse had nowhere to land.

A MIXIN rather than a second object, for `deploy_state_k8s`'s reason — every caller holds a
`DeployerState` and calls `state.alerted_sha(...)` directly, so inheritance keeps that surface
byte-identical. It reads and writes through `DeployerState.read`/`write`, which it does not
define and must not: the marker files, their atomic writes and the `MARKERS` table stay in one
place.

Stdlib only, plus `gitops_markers` — the same leaf contract `deploy_state` carries.
"""

from typing import TYPE_CHECKING

from gitops_markers import ALERT_SLOTS, format_alerted, parse_alerted


class AlertSlotMarkers:
    """The `alerted` marker: read, record and clear one channel's dedupe SHA.

    Mixed into `deploy_state.DeployerState`, which supplies `read`, `write` and `directory`.
    """

    if TYPE_CHECKING:
        # Supplied by `deploy_state.DeployerState`; declared so the checker sees the surface
        # this mixin relies on. Never defined at runtime, so the real methods win.
        def read(self, marker: str) -> str | None: ...

        def write(self, marker: str, value: str | None) -> None: ...

    def alerted_sha(self, slot: str) -> str | None:
        """The origin SHA `slot` last paged on, or None when it has paged on nothing.

        Raises:
            KeyError: `slot` is not an `ALERT_SLOTS` member — a typo is a mistake, not a new
                channel. The check `path()` gave each slot while it was its own marker.
        """
        return parse_alerted(self.read("alerted")).get(self._alert_slot(slot))

    def record_alerted(self, slot: str, sha: str) -> None:
        """Record that `slot` has now paged on `sha`, leaving every other slot alone."""
        alerted = parse_alerted(self.read("alerted"))
        alerted[self._alert_slot(slot)] = sha
        self.write("alerted", format_alerted(alerted))

    def clear_alerted(self, slot: str) -> None:
        """Drop `slot`'s line, so the next tick pages on that SHA again.

        The marker is removed when `slot` held the last line, as every other line-oriented
        marker here is: an empty file and no file read the same.
        """
        alerted = parse_alerted(self.read("alerted"))
        if alerted.pop(self._alert_slot(slot), None) is None:
            return
        self.write("alerted", format_alerted(alerted))

    @staticmethod
    def _alert_slot(slot: str) -> str:
        """`slot` itself, or `KeyError` when it is not one of `ALERT_SLOTS`."""
        if slot not in ALERT_SLOTS:
            raise KeyError(slot)
        return slot
