# ansible/roles/setup/gitops_deploy/files/deploy_state_k8s.py
"""The two k8s owed-work families, as a mixin `deploy_state.DeployerState` inherits.

`k8s_deferred` holds the image bumps a broad tick merged and could not deploy; `k8s_unapplied`
holds the k8s role changes a tick merged and will never apply. Both are one entry per service,
and the only difference between recording one and the other is whether an already-listed
service's entry advances to the new origin. That shared shape is the seam this module is split
along (#2663): `deploy_state.py` stood at the 600-line module cap, so the next change to a
marker family had nowhere to land.

Both are classes of the `owed` JSON-lines ledger (#3392). `k8s_unapplied` moved first, since
nothing pages on it. `k8s_deferred` moved once every reader copy read its class, and its line
marker went once daniel-box held no legacy file.

It is a MIXIN rather than a second object because every caller reaches these methods as
`state.<method>` — `deploy_defer`, `deploy_broad_k8s` and `scripts/deploy_tools/gitops_state.py`
all hold a `DeployerState` and call it directly. Inheritance keeps that surface byte-identical;
a helper object would rename every call site. The private methods read and write through
`DeployerState.read`/`write`, which the mixin does not define and must not: the marker files,
their atomic writes and the `MARKERS` table stay in one place.

Stdlib only, plus `gitops_markers` and `gitops_ledger` — the leaf contract `deploy_state`
carries.
"""

from gitops_ledger import (
    OWED_K8S_DEFERRED,
    OWED_K8S_UNAPPLIED,
    drop_owed,
    k8s_deferred_entries,
    k8s_unapplied_entries,
    owed_line,
    parse_owed,
    rewrite_owed,
)
from gitops_markers import K8sDeferredEntry


class K8sLineMarkers:
    """`k8s_deferred` and `k8s_unapplied`: read, record and clear, one entry per service.

    Mixed into `deploy_state.DeployerState`, which supplies `read` and `write`.
    """

    def _record_owed(
        self,
        owed: str | None,
        cls: str,
        origin: str,
        services,
        now: float,
        advance: bool,
    ) -> list[str]:
        """Append a `cls` line per service the ledger `owed` does not list yet. Returns those.

        `rewrite_owed` owns the repair of a TORN line naming one of `services` (#2657), and
        the move to `origin` under `advance`. A repaired service reads as listed, so this
        updates its line rather than appending a second one beside it.
        """
        wanted = set(services)
        text = rewrite_owed(owed, cls, wanted, origin, now, advance)
        added = sorted(wanted - {e.subject for e in parse_owed(text, cls)})
        lines = text.splitlines() + [owed_line(cls, s, origin, now) for s in added]
        if added or text != (self.read("owed") or ""):
            self.write("owed", "\n".join(lines))
        return added

    # ── the image bumps a broad tick merged and could not deploy (#2449), in the ledger ──
    # The paging half: monitor-bridge pages on the oldest entry past its age gate.

    def k8s_deferred_pending(self) -> list[K8sDeferredEntry]:
        """Every bump still owed, oldest entry first."""
        return k8s_deferred_entries(self.read("owed"))

    def record_k8s_deferred(self, origin: str, services, now: float) -> list[str]:
        """Record the bumps this tick merged and could not deploy. Returns the ones added.

        A service already listed keeps its first-seen stamp, the age monitor-bridge pages on.
        IT KEEPS ITS ORIGIN TOO, where `record_k8s_unapplied` advances it (#2644): nothing
        compares this class's SHA, and `deploy_defer.unrecord` can reset the merge.
        """
        return self._record_owed(
            self.read("owed"), OWED_K8S_DEFERRED, origin, services, now, advance=False
        )

    def clear_k8s_deferred(self, services) -> list[str]:
        """Drop the `k8s_deferred` entries naming any of `services`. Returns the names cleared.

        A torn line naming one of `services` goes as well (#2657).
        """
        text, cleared = drop_owed(self.read("owed"), OWED_K8S_DEFERRED, services)
        if cleared:
            self.write("owed", text or None)
        return cleared

    # ── the k8s changes a tick merged and will never apply (#2570), in the ledger (#3392) ──
    # The non-paging half: the SessionStart banner and the journal read it, nothing else.

    def k8s_unapplied_pending(self) -> list[K8sDeferredEntry]:
        """Every k8s role change still owed, oldest entry first."""
        return k8s_unapplied_entries(self.read("owed"))

    def record_k8s_unapplied(self, origin: str, services, now: float) -> list[str]:
        """Record the k8s role changes this tick merged and will never apply. Returns the added.

        A SERVICE ALREADY LISTED HAS ITS ENTRY MOVED TO `origin` (#2644):
        `deploy_defer.discharge_k8s_unapplied` drops an entry once a release record descends
        from the SHA it names, so one left at the OLDEST origin discharges over every later
        change to the same service. The first-seen stamp stays: it dates the oldest change.
        That moved entry stays OUT of the return value, which `deploy_defer.unrecord` clears:
        an entry predating the tick survives the reset.
        """
        return self._record_owed(
            self.read("owed"), OWED_K8S_UNAPPLIED, origin, services, now, advance=True
        )

    def clear_k8s_unapplied(self, services) -> list[str]:
        """Drop the `k8s_unapplied` entries naming any of `services`. Returns the names cleared.

        A torn ledger line naming one of `services` goes as well (#2657).
        """
        text, cleared = drop_owed(self.read("owed"), OWED_K8S_UNAPPLIED, services)
        if cleared:
            self.write("owed", text or None)
        return cleared
