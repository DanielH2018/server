# ansible/roles/setup/gitops_deploy/files/deploy_state_alerts.py
"""The per-SHA alert dedupe slots, as a mixin `deploy_state.DeployerState` inherits.

One `alerted` marker holds every channel as a `"<slot> <origin_sha>"` line, and
`gitops_markers.ALERT_SLOTS` is the set of slots. Seven `<channel>_alerted_sha` files held the
same state before #3047; `migrate_alerted` is what folds a live host's copies in.

Split out for the reason `deploy_state_k8s` was (#2663): `deploy_state.py` stood at the
600-line module cap, so the collapse had nowhere to land.

A MIXIN rather than a second object, for `deploy_state_k8s`'s reason — every caller holds a
`DeployerState` and calls `state.alerted_sha(...)` directly, so inheritance keeps that surface
byte-identical. It reads and writes through `DeployerState.read`/`write`, which it does not
define and must not: the marker files, their atomic writes and the `MARKERS` table stay in one
place.

Stdlib only, plus `gitops_markers` — the same leaf contract `deploy_state` carries.
"""

import os

from deploy_config import log
from gitops_markers import (
    ALERT_SLOTS,
    LEGACY_ALERT_MARKERS,
    format_alerted,
    parse_alerted,
)


class AlertSlotMarkers:
    """The `alerted` marker: read, record and clear one channel's dedupe SHA.

    Mixed into `deploy_state.DeployerState`, which supplies `read`, `write` and `directory`.
    """

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

    def migrate_alerted(self) -> list[str]:
        """Fold any pre-#3047 `<channel>_alerted_sha` file into the keyed marker, then remove it.

        Returns:
            The slots migrated, sorted — empty on every tick after the first.

        Live hosts hold the seven old files, and a tick that read only the collapsed marker
        would find nothing and re-page every SHA it had already paged on. So the FIRST tick
        after this lands imports them, ahead of the drain and of every channel's own read.

        Idempotent and crash-safe in that order: the keyed marker is written (atomically)
        before any old file is removed, and a slot it ALREADY holds is left alone rather than
        overwritten. A death between the write and the removals leaves the old files to be
        re-read and dropped by the next tick, which imports nothing and reaches the same end
        state. Gating the whole import on the collapsed file being absent would instead strand
        those files forever.
        """
        legacy = {}
        for slot, basename in LEGACY_ALERT_MARKERS.items():
            path = os.path.join(self.directory, basename)
            try:
                with open(path) as fh:
                    value = fh.read().strip()
            except FileNotFoundError:
                continue
            legacy[slot] = (path, value or None)
        if not legacy:
            return []
        alerted = parse_alerted(self.read("alerted"))
        migrated = [
            slot
            for slot, (_, value) in sorted(legacy.items())
            if value is not None and slot not in alerted
        ]
        for slot in migrated:
            alerted[slot] = legacy[slot][1]
        if migrated:
            self.write("alerted", format_alerted(alerted))
            log(f"alert markers collapsed into one file: {', '.join(migrated)}")
        for path, _ in legacy.values():
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return migrated

    @staticmethod
    def _alert_slot(slot: str) -> str:
        """`slot` itself, or `KeyError` when it is not one of `ALERT_SLOTS`."""
        if slot not in ALERT_SLOTS:
            raise KeyError(slot)
        return slot
