"""Pins the relationship between k8s/volume-revert's per-claim timeouts and the rollback
redeploy's own budget (task 6b).

WHY THIS EXISTS. The rollback redeploy's `K8S_ROLLBACK_TIMEOUT_S`
(`gitops_deploy_k8s_rollback_timeout_s`) has to cover the worst case a real service's REVERT can
hit: `volume_revert_state_timeout` and `volume_revert_api_timeout` bound one claim's three state
waits and three API calls, and a service can declare more than one claim (`tdarr`,
`code-server` each declare two, via `k8s_autodeploy_snapshot_pvcs`). Nothing enforced that
relationship until this test — a later change to either side (a bumped per-claim timeout, a
service declaring a third claim, a cut to the rollback budget) could silently reopen the gap
Task 6's drill was measuring against, and every other test would stay green.

WHAT THIS DOES NOT COVER, on purpose. The same `ansible-playbook` run this timeout bounds also
pays a per-claim snapshot wait (`volume_snapshot_timeout`, 120s) BEFORE the revert, then an
apply and a rollout wait AFTER it. This test's margin floor is sized against the drill's
REALISTIC overhead for those (Phase 4: ~5.5s snapshot wait + ~32s rollout-drain wait for one
claim), not against every one of those steps ALSO hitting its own worst-case ceiling at once.
That fully compound case — snapshot AND revert AND rollout all independently stalling to their
ceilings in the same run — is not proven to fit inside 900s and is an accepted residual risk
(see `ansible/roles/setup/gitops_deploy/CLAUDE.md`'s rollback-timeout section), consistent with
how `gitops_deploy_k8s_timeout_s` itself is sized against realistic per-service time rather than
worst-case ceiling-stacking across independent mechanisms.

Sized against `ansible/roles/k8s/volume-revert/CLAUDE.md`'s task-6 numbers: at
`volume_revert_state_timeout`/`volume_revert_api_timeout` = 90/30, a two-claim service's
worst-case revert is 720s, inside the 900s `gitops_deploy_k8s_rollback_timeout_s` with 180s left
for the realistic (not worst-case) cost of the rest of that run.
"""

from __future__ import annotations

import pathlib

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
_K8S_ROLES = _REPO / "ansible/roles/k8s"
_VOLUME_REVERT_DEFAULTS = _K8S_ROLES / "volume-revert/defaults/main.yml"
_GITOPS_DEPLOY_DEFAULTS = _REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"

# Realistic (not worst-case) overhead the rollback redeploy pays beyond the revert itself: the
# task-6 drill's Phase 4 measured ~5.5s snapshot wait + ~32s rollout-drain wait for ONE claim
# (~38s total; the rollout wait is per-SERVICE, not per-claim, so it doesn't scale with claim
# count). This floor is roughly 2x that observed figure, not a ceiling-stacked worst case — see
# the module docstring for what a fully compound worst case would need instead.
_MIN_REALISTIC_OVERHEAD_MARGIN_S = 90


def _max_declared_claims() -> int:
    """The most claims any role declares in `k8s_autodeploy_snapshot_pvcs` — the shape a
    multi-claim service like `tdarr`/`code-server` actually has today, read from the roles
    rather than hardcoded so a role adding a third claim trips this test instead of drifting
    past it unnoticed."""
    counts = []
    for defaults in sorted(_K8S_ROLES.glob("*/defaults/main.yml")):
        declared = (yaml.safe_load(defaults.read_text()) or {}).get(
            "k8s_autodeploy_snapshot_pvcs"
        )
        if declared:
            counts.append(len(declared))
    assert counts, (
        "found no roles declaring k8s_autodeploy_snapshot_pvcs; the reader is broken"
    )
    return max(counts)


def test_rollback_timeout_covers_the_worst_case_revert_with_realistic_overhead_margin():
    revert_defaults = yaml.safe_load(_VOLUME_REVERT_DEFAULTS.read_text())
    gitops_defaults = yaml.safe_load(_GITOPS_DEPLOY_DEFAULTS.read_text())

    state_timeout = int(revert_defaults["volume_revert_state_timeout"])
    api_timeout = int(revert_defaults["volume_revert_api_timeout"])
    rollback_timeout = int(gitops_defaults["gitops_deploy_k8s_rollback_timeout_s"])
    max_claims = _max_declared_claims()

    # Three state waits and three API calls per claim (scale-down's own detach wait, the
    # maintenance-attach wait, the post-revert detach wait; the maintenance-attach POST, the
    # revert POST, the detach POST) — see k8s/volume-revert/CLAUDE.md's sequence table.
    worst_case_revert = max_claims * 3 * (state_timeout + api_timeout)

    assert worst_case_revert <= rollback_timeout, (
        f"a {max_claims}-claim service's worst-case revert ({worst_case_revert}s = "
        f"{max_claims} claims x 3 x ({state_timeout}s state + {api_timeout}s api)) exceeds "
        f"gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s) — the rollback redeploy "
        f"would get SIGTERMed mid-revert, stranding the service with its workload at zero and "
        f"a volume possibly still in maintenance mode."
    )

    margin = rollback_timeout - worst_case_revert
    assert margin >= _MIN_REALISTIC_OVERHEAD_MARGIN_S, (
        f"only {margin}s of gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s) is left "
        f"after a {max_claims}-claim service's worst-case revert ({worst_case_revert}s) — below "
        f"the {_MIN_REALISTIC_OVERHEAD_MARGIN_S}s floor this test holds for the rollback "
        f"redeploy's REALISTIC (not worst-case) snapshot wait, apply and rollout. Raise "
        f"gitops_deploy_k8s_rollback_timeout_s (and recheck it against "
        f"gitops-deploy.service.j2's TimeoutStartSec) rather than shrinking this floor."
    )
