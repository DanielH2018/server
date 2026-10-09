# ansible/roles/setup/gitops_deploy/files/deploy_state_k8s.py
"""The two k8s owed-work classes, as a mixin `deploy_state.DeployerState` inherits.

`k8s_deferred` holds the image bumps a broad tick merged and could not deploy; `k8s_unapplied`
holds the k8s role changes a tick merged and will never apply. Both are one entry per service,
and the only difference between recording one and the other is whether an already-listed
service's entry advances to the new origin. That shared shape is the seam this module is split
along (#2663): `deploy_state.py` stood at the 600-line module cap, so the next change to a
marker family had nowhere to land.

Both are classes of the `owed` JSON-lines ledger (#3392). `k8s_unapplied` moved first, since
nothing pages on it. `k8s_deferred` moved once every reader copy read its class, and its line
marker went once daniel-box held no legacy file.

One record/clear/pending trio serves both, keyed by class (#3544). `_K8S_OWED` is the table of
what differs per class; a class it does not name raises rather than falling back, because a
default would write a line with another class's semantics.

It is a MIXIN rather than a second object because every caller reaches these methods as
`state.<method>` — `deploy_defer`, `deploy_broad_k8s` and `scripts/deploy_tools/gitops_state.py`
all hold a `DeployerState` and call it directly. The private methods read and write through
`DeployerState.read`/`write`, which the mixin does not define and must not: the marker files,
their atomic writes and the `MARKERS` table stay in one place.

Stdlib only, plus `gitops_markers` and `gitops_ledger` — the leaf contract `deploy_state`
carries.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

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


class _K8sOwedClass(NamedTuple):
    """What one k8s owed class does differently from the other."""

    # Reads the class's entries out of the ledger text. The two readers differ: a deferred
    # service on two lines keeps its OLDER entry, the age monitor-bridge pages on.
    entries: Callable[[str | None], list[K8sDeferredEntry]]
    # Whether recording an already-listed service moves its entry to the new origin (#2644).
    advance: bool


_K8S_OWED: dict[str, _K8sOwedClass] = {
    # The image bumps a broad tick merged and could not deploy (#2449). The paging class:
    # monitor-bridge pages on the oldest entry past its age gate. An entry KEEPS ITS ORIGIN:
    # nothing compares this class's SHA, and `deploy_defer.unrecord` can reset the merge.
    OWED_K8S_DEFERRED: _K8sOwedClass(k8s_deferred_entries, advance=False),
    # The k8s changes a tick merged and will never apply (#2570). Nothing pages on it; the
    # SessionStart banner and the journal read it. An entry MOVES TO THE NEW ORIGIN:
    # `deploy_k8s_owed.discharge_k8s_unapplied` drops an entry once a release record descends
    # from the SHA it names, so one left at the OLDEST origin discharges over every later
    # change to the same service.
    OWED_K8S_UNAPPLIED: _K8sOwedClass(k8s_unapplied_entries, advance=True),
}


def _k8s_owed(cls: str) -> _K8sOwedClass:
    """The table row for `cls`, or ValueError for a class this mixin does not own."""
    try:
        return _K8S_OWED[cls]
    except KeyError:
        raise ValueError(
            f"{cls!r} is not a k8s owed class; expected one of {sorted(_K8S_OWED)}"
        ) from None


class K8sLineMarkers:
    """`k8s_deferred` and `k8s_unapplied`: read, record and clear, one entry per service.

    Mixed into `deploy_state.DeployerState`, which supplies `read` and `write`.
    """

    if TYPE_CHECKING:
        # Supplied by `deploy_state.DeployerState`; declared so the checker sees the surface
        # this mixin relies on. Never defined at runtime, so the real methods win.
        def read(self, marker: str) -> str | None: ...

        def write(self, marker: str, value: str | None) -> None: ...

    def owed_pending(self, cls: str) -> list[K8sDeferredEntry]:
        """Every `cls` entry still owed. `k8s_deferred` comes back oldest entry first."""
        return _k8s_owed(cls).entries(self.read("owed"))

    def record_owed(self, cls: str, origin: str, services, now: float) -> list[str]:
        """Record a `cls` entry per service in `services`. Returns the ones added.

        A service already listed keeps its first-seen stamp, which dates the oldest change.
        Under `k8s_unapplied` its entry moves to `origin`; under `k8s_deferred` it keeps its
        origin too. A moved entry stays OUT of the return value, which
        `deploy_defer.unrecord` clears: an entry predating the tick survives the reset.

        `rewrite_owed` owns the repair of a TORN line naming one of `services` (#2657), and
        the move to `origin`. A repaired service reads as listed, so this updates its line
        rather than appending a second one beside it.
        """
        advance = _k8s_owed(cls).advance
        owed = self.read("owed")
        wanted = set(services)
        text = rewrite_owed(owed, cls, wanted, origin, now, advance)
        added = sorted(wanted - {e.subject for e in parse_owed(text, cls)})
        lines = text.splitlines() + [owed_line(cls, s, origin, now) for s in added]
        if added or text != (owed or ""):
            self.write("owed", "\n".join(lines))
        return added

    def clear_owed(self, cls: str, services) -> list[str]:
        """Drop the `cls` entries naming any of `services`. Returns the names cleared.

        A torn ledger line naming one of `services` goes as well (#2657).
        """
        _k8s_owed(cls)
        text, cleared = drop_owed(self.read("owed"), cls, services)
        if cleared:
            self.write("owed", text or None)
        return cleared
