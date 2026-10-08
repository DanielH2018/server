# ansible/roles/setup/gitops_deploy/files/deploy_state_hold.py
"""The hold: `hold_sha` and the planes it waits on, the `owed` ledger's `hold_plane` class.

A failed broad apply holds its SHA and records the plane it failed in, and an apply that
covers a plane drops it; `hold_sha` clears once no plane is left (#878). The planes moved
from the `; `-joined `hold_plane` line marker into the `owed` ledger (#3392); the marker,
and the fold that carried its last entries over, are retired.

A MIXIN, like `deploy_state_k8s.K8sLineMarkers`, and for the same reason: every caller
reaches these methods as `state.<method>`, and `deploy_state.py` stood at the 600-line module
cap. Mixed into `deploy_state.DeployerState`, which supplies `read`, `write` and `hold_sha`.

Stdlib only, plus `deploy_config` for `log`, `deploy_git` for the pure hold decisions, and
`gitops_ledger` — the leaf contract `deploy_state` carries.
"""

import time
from typing import TYPE_CHECKING

from deploy_config import log
from deploy_git import HOLD_PLANE_SEP, broad_hold_cleared_by, hold_plane_marker
from gitops_ledger import (
    OWED_HOLD_PLANE,
    drop_owed,
    held_planes,
    owed_line,
    owed_line_key,
    parse_owed,
    rewrite_owed,
)


class HoldMarkers:
    """The hold, as methods on the state object.

    Mixed into `deploy_state.DeployerState`, which supplies `read`, `write` and `hold_sha`.
    """

    if TYPE_CHECKING:
        # Supplied by `deploy_state.DeployerState`; declared so the checker sees the surface
        # this mixin relies on. Never defined at runtime, so the real methods win.
        def read(self, marker: str) -> str | None: ...

        def write(self, marker: str, value: str | None) -> None: ...

    @property
    def hold_plane(self) -> str | None:
        """Every plane the hold waits on, `; `-joined, or None.

        Read through `gitops_ledger.held_planes`, the query every other reader uses.
        """
        return HOLD_PLANE_SEP.join(held_planes(self.read("owed"))) or None

    # ── holding, and the two ways a hold clears ───────────────────────────────────────────

    def write_hold(self, sha: str | None) -> None:
        """Record `sha` as the commit this host refuses to redeploy, or clear the hold."""
        self.write("hold", sha)

    # The `owed` ledger's `hold_plane` class (#3392), one line per failed apply. The subject
    # is `deploy_git.hold_plane_marker`'s `<playbook> <tags>` text, and the origin is the SHA
    # the hold was written for.

    @staticmethod
    def _held_subjects(owed: str | None) -> list[str]:
        """Every subject a `hold_plane` ledger line names, torn lines included, in file order.

        A TORN line counts. The hold-clear rule reads this list, and a plane it skipped would
        let `hold_sha` clear while that plane is still unapplied (#878's class).
        """
        keys = (owed_line_key(line) for line in (owed or "").splitlines())
        subjects = [k[1].strip() for k in keys if k and k[0] == OWED_HOLD_PLANE]
        return list(dict.fromkeys(subjects))

    def hold_failed_apply(self, sha: str, playbook: str, tags: list[str]) -> None:
        """Hold `sha` for a failed apply of `playbook`/`tags`, beside any plane already held.

        Added to the ledger's `hold_plane` class, never written over an entry already there.
        A second failure used to OVERWRITE the plane, so the first plane's entry was gone
        while that plane was still unapplied: the second failure's fix then cleared
        `hold_sha` over it, and GitOps Deploy — Status read green (#878's class). Each entry
        clears on its own. An entry already listed keeps its origin and first-seen stamp.
        """
        now = time.time()
        owed = self.read("owed")
        self.write_hold(sha)
        entry = hold_plane_marker(playbook, tags)
        # `rewrite_owed` repairs a TORN line naming the entry (#2657) rather than leaving it
        # beside a second, readable one.
        text = rewrite_owed(owed, OWED_HOLD_PLANE, [entry], sha, now, advance=False)
        if entry not in {e.subject for e in parse_owed(text, OWED_HOLD_PLANE)}:
            text = "\n".join(
                [*text.splitlines(), owed_line(OWED_HOLD_PLANE, entry, sha, now)]
            )
        if text != (owed or ""):
            self.write("owed", text)

    def clear_broad_hold(self, playbook: str, tags: list[str]) -> None:
        """Clear the hold after a broad apply, but only once no held plane is left unapplied.

        A hold says a plane is unapplied, and every consumer gates on `hold_sha` — so
        clearing it after a success in a DIFFERENT plane turns GitOps Deploy — Status green
        over a plane nothing has applied (issue #878). This apply drops the ledger entries
        it covers; while one survives, the tick still succeeded and the hold is kept.

        The covered entries go BEFORE `hold_sha`. A crash between the two leaves a hold
        with fewer planes, which the next apply clears, rather than planes with no hold.
        """
        owed = self.read("owed")
        held = self._held_subjects(owed)
        left = [e for e in held if not broad_hold_cleared_by(e, playbook, tags)]
        if left != held:
            text, _ = drop_owed(owed, OWED_HOLD_PLANE, set(held) - set(left))
            self.write("owed", text or None)
        if left:
            log(
                f"hold kept: {HOLD_PLANE_SEP.join(left)} is still unapplied "
                f"(this tick applied {hold_plane_marker(playbook, tags)})"
            )
            return
        self.write_hold(None)

    def clear_service_hold(self, services: set[str]) -> None:
        """Clear a hold after a successful service deploy, unless it leaves a plane unapplied.

        A k8s deploy is `ansible/deploy.yml --tags <services>`, so it drops a held
        entry naming that playbook at a subset of those tags — a failed bump on a broad tick
        writes exactly that, and the fix-forward deploy of the same service is its way out.
        Any other entry stays held: without this, an unrelated service deploy clears
        `hold_sha` and orphans the held planes, which `gitops_status` never reads on its own.
        """
        if not services:
            # Torn lines included, as in `clear_broad_hold`: an untagged deploy.yml covers
            # every deploy.yml plane, so a torn one skipped here would clear over nothing.
            held = self._held_subjects(self.read("owed"))
            if held:
                log(f"hold kept: {HOLD_PLANE_SEP.join(held)} is still unapplied")
                return
        self.clear_broad_hold("ansible/deploy.yml", sorted(services))
