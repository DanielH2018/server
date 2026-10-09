# ansible/roles/setup/gitops_deploy/files/deploy_state_hold.py
"""The hold, as methods on the deployer's state object, delegating to `gitops_hold.Hold`.

`gitops_hold` owns the rule that `hold_sha` clears only together with its `hold_plane` ledger
lines, and the deploy UI's Clear calls the same module (#3658). This mixin keeps the names
every tick caller reaches as `state.<method>` and adds the journal lines a tick prints when a
hold is kept.

A MIXIN, like `deploy_state_k8s.K8sLineMarkers`, and for the same reason: every caller
reaches these methods as `state.<method>`, and `deploy_state.py` stood at the 600-line module
cap. Mixed into `deploy_state.DeployerState`, which supplies `directory`.

Stdlib only, plus `deploy_config` for `log` and `gitops_hold`.
"""

from deploy_config import log
from gitops_hold import HOLD_PLANE_SEP, Hold, hold_plane_marker


class HoldMarkers:
    """The hold, as methods on the state object.

    Mixed into `deploy_state.DeployerState`, which supplies `directory`.
    """

    directory: str

    @property
    def _hold(self) -> Hold:
        return Hold(self.directory)

    @property
    def hold_plane(self) -> str | None:
        """Every plane the hold waits on, `; `-joined, or None."""
        return HOLD_PLANE_SEP.join(self._hold.planes()) or None

    def write_hold(self, sha: str | None) -> None:
        """Record `sha` as the commit this host refuses to redeploy, or clear the hold."""
        self._hold.set_sha(sha)

    def hold_failed_apply(self, sha: str, playbook: str, tags: list[str]) -> None:
        """Hold `sha` for a failed apply of `playbook`/`tags`, beside any plane already held."""
        self._hold.record(sha, playbook, tags)

    def clear_broad_hold(self, playbook: str, tags: list[str]) -> None:
        """Clear the hold after a broad apply, but only once no held plane is left unapplied.

        A hold says a plane is unapplied, and every consumer gates on `hold_sha` — so
        clearing it after a success in a DIFFERENT plane turns GitOps Deploy — Status green
        over a plane nothing has applied (issue #878). `Hold.cover` drops the entries this
        apply covers; while one survives, the tick still succeeded and the hold is kept.
        """
        left = self._hold.cover(playbook, tags)
        if left:
            log(
                f"hold kept: {HOLD_PLANE_SEP.join(left)} is still unapplied "
                f"(this tick applied {hold_plane_marker(playbook, tags)})"
            )

    def clear_service_hold(self, services: set[str]) -> None:
        """Clear a hold after a successful service deploy, unless it leaves a plane unapplied.

        A k8s deploy is `ansible/deploy.yml --tags <services>`, so it drops a held
        entry naming that playbook at a subset of those tags — a failed bump on a broad tick
        writes exactly that, and the fix-forward deploy of the same service is its way out.
        Any other entry stays held: without this, an unrelated service deploy clears
        `hold_sha` and orphans the held planes, which `gitops_status` never reads on its own.
        """
        left = self._hold.cover_services(services)
        if not left:
            return
        applied = hold_plane_marker("ansible/deploy.yml", sorted(services))
        suffix = f" (this tick applied {applied})" if services else ""
        log(f"hold kept: {HOLD_PLANE_SEP.join(left)} is still unapplied{suffix}")
