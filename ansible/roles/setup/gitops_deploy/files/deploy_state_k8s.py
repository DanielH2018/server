# ansible/roles/setup/gitops_deploy/files/deploy_state_k8s.py
"""The two k8s marker families, as a mixin `deploy_state.DeployerState` inherits.

`k8s_deferred` holds the image bumps a broad tick merged and could not deploy; `k8s_unapplied`
holds the k8s role changes a tick merged and will never apply. Both are one line per service,
both parse with `gitops_markers.parse_k8s_deferred`, and the only difference between recording
one and the other is whether an already-listed service's line advances to the new origin. That
shared shape is the seam this module is split along (#2663): `deploy_state.py` stood at the
600-line module cap, so the next change to a marker family had nowhere to land.

It is a MIXIN rather than a second object because every caller reaches these six methods as
`state.<method>` — `deploy_defer`, `deploy_broad_k8s` and `scripts/deploy_tools/gitops_state.py`
all hold a `DeployerState` and call it directly. Inheritance keeps that surface byte-identical;
a helper object would rename every call site. The three private methods read and write through
`DeployerState.read`/`write`, which the mixin does not define and must not: the marker files,
their atomic writes and the `MARKERS` table stay in one place.

Stdlib only, plus `gitops_markers` — the same leaf contract `deploy_state` carries.
"""

from gitops_markers import (
    K8sDeferredEntry,
    k8s_line_service,
    parse_k8s_deferred,
    rewrite_k8s_lines,
)


class K8sLineMarkers:
    """`k8s_deferred` and `k8s_unapplied`: read, record and clear, one line per service.

    Mixed into `deploy_state.DeployerState`, which supplies `read` and `write`.
    """

    def _k8s_line_pending(self, marker: str) -> list[K8sDeferredEntry]:
        """Every line `marker` still holds, oldest first."""
        return parse_k8s_deferred(self.read(marker))

    def _record_k8s_line(
        self, marker: str, origin: str, services, now: float, advance: bool = False
    ) -> list[str]:
        """Append a line per service `marker` does not list yet. Returns the ones added.

        `advance` also moves an ALREADY-LISTED service's line to `origin`, keeping its
        first-seen stamp (#2644). It stays OUT of the return value, which
        `deploy_defer.unrecord` clears: a line predating the tick survives the reset.

        `rewrite_k8s_lines` owns both the advance and the repair of a TORN line naming one of
        `services` (#2657): a repaired service reads as listed, so this updates its line rather
        than appending a second one beside it.
        """
        wanted = set(services)
        text = rewrite_k8s_lines(self.read(marker), wanted, origin, now, advance)
        added = sorted(wanted - {e.service for e in parse_k8s_deferred(text)})
        lines = text.splitlines() + [f"{origin} {s} {now:.0f}" for s in added]
        if added or text != (self.read(marker) or ""):
            self.write(marker, "\n".join(lines))
        return added

    def _clear_k8s_lines(self, marker: str, services) -> list[str]:
        """Drop `marker`'s lines naming any of `services`. Returns the names cleared.

        A TORN LINE NAMING ONE OF `services` GOES TOO (#2657), where a line naming nobody is
        carried through untouched: dropping that one loses the only record that something was
        deferred, and a clear of every service would still leave it standing forever.
        """
        wanted = set(services)
        kept, cleared = [], []
        for line in (self.read(marker) or "").splitlines():
            service = k8s_line_service(line)
            if service in wanted:
                cleared.append(service)
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

        A service already listed keeps its first-seen stamp, the age monitor-bridge pages on.
        IT KEEPS ITS ORIGIN TOO, where `record_k8s_unapplied` advances it (#2644): nothing
        compares this marker's SHA, and `deploy_defer.unrecord` can reset the merge.
        """
        return self._record_k8s_line("k8s_deferred", origin, services, now)

    def clear_k8s_deferred(self, services) -> list[str]:
        """Drop the `k8s_deferred` lines naming any of `services`. Returns the names cleared."""
        return self._clear_k8s_lines("k8s_deferred", services)

    # ── the k8s changes a tick merged and will never apply (#2570) ────────────────────────
    # The non-paging half: the SessionStart banner and the journal read it, nothing else.

    def k8s_unapplied_pending(self) -> list[K8sDeferredEntry]:
        """Every k8s role change the `k8s_unapplied` marker still holds, oldest line first."""
        return self._k8s_line_pending("k8s_unapplied")

    def record_k8s_unapplied(self, origin: str, services, now: float) -> list[str]:
        """Record the k8s role changes this tick merged and will never apply. Returns the added.

        A SERVICE ALREADY LISTED HAS ITS LINE MOVED TO `origin` (#2644):
        `deploy_defer.discharge_k8s_unapplied` drops a line once a release record descends
        from the SHA it names, so one left at the OLDEST origin discharges over every later
        change to the same service. The first-seen stamp stays: it dates the oldest change.
        """
        return self._record_k8s_line(
            "k8s_unapplied", origin, services, now, advance=True
        )

    def clear_k8s_unapplied(self, services) -> list[str]:
        """Drop the `k8s_unapplied` lines naming any of `services`. Returns the names cleared."""
        return self._clear_k8s_lines("k8s_unapplied", services)
