"""Pins the relationship between k8s/volume-revert's per-claim timeouts and the rollback
redeploy's own budget (task 6b).

WHY THIS EXISTS. The rollback redeploy's `K8S_ROLLBACK_TIMEOUT_S`
(`gitops_deploy_k8s_rollback_timeout_s`) has to cover `worst_case_revert` below: the longest a
two-claim service's revert can run before its own fate (success or failure) is decided.
`volume_revert_state_timeout` and `volume_revert_api_timeout` bound one claim's three state
waits and three API calls, and a service can declare more than one claim (`tdarr`,
`code-server` each declare two, via `k8s_autodeploy_snapshot_pvcs`). Nothing enforced that
relationship until this test — a later change to either side (a bumped per-claim timeout, a
service declaring a third claim, a cut to the rollback budget) could silently reopen the gap
Task 6's drill was measuring against, and every other test would stay green.

WHAT `worst_case_revert` MEANS, precisely, because it is easy to misread. Every wait in
`k8s/volume-revert/tasks/claim.yml` is `until:` with no `ignore_errors` and no `failed_when`, so
an exhausted wait fails the task and the WHOLE PLAY stops there — a failure in claim 1 means
claim 2 never runs. That means `max_claims * 3 * (state + api)` is NOT "every wait across every
claim times out and processing continues anyway" — that scenario is impossible. It IS reached
two other ways that consume the same wall time: every wait/call across every claim succeeds
right at its own ceiling, or every one succeeds except the very last, which fails after
consuming the same ceiling. Both are real, and neither compounds a failure from one claim (or
phase) with an independent failure from another.

WHAT THIS DOES NOT COVER, on purpose. The same `ansible-playbook` run this timeout bounds also
pays a per-claim snapshot wait (`volume_snapshot_timeout`, 120s) BEFORE the revert, then an
apply and a rollout wait AFTER it. On a genuinely slow-but-successful run — nothing fails, so
nothing stops the play early — those are ADDITIVE to `worst_case_revert` on one continuous
timeline. This test's margin floor is sized against the drill's REALISTIC overhead for those
(Phase 4: ~5.5s snapshot wait + ~32s rollout-drain wait for one claim), not against a slow
success in every one of those steps too. That combined slow-success total is not proven to fit
inside 900s and is an accepted residual risk (see `ansible/roles/setup/gitops_deploy/CLAUDE.md`'s
rollback-timeout section) — a narrower and smaller risk than a compounding-failure scenario,
which the abort-on-first-failure semantics above rule out entirely.

Sized against `ansible/roles/k8s/volume-revert/CLAUDE.md`'s task-6 numbers: at
`volume_revert_state_timeout`/`volume_revert_api_timeout` = 90/30, a two-claim service's
`worst_case_revert` is 720s, inside `gitops_deploy_k8s_rollback_timeout_s` — which is 1320s, not
the 900s this docstring claimed until 2026-08-22, leaving 600s rather than 180s for the
realistic (not worst-case) cost of the rest of that run. The assertions always read the live
YAML, so only this prose was ever stale — but it is the prose an operator reads when the test
fails, which is the worst moment to hand them a wrong number.

THE MULTI-SERVICE AXIS (2026-08-22 review H2). Everything above reasons per SERVICE and about
CLAIMS within it. That is the wrong axis for the batch: one tick can promote several services
into a single playbook run, each paying its own snapshot+revert phase serially, so the cost is
additive across services while this budget covers one. The claims-only arithmetic here passed
green throughout, which is exactly why the gap survived. `test_batch_of_claim_services_fits_the_
rollback_budget` below closes it by reading the per-tick cap, and it takes the rollout term as a
`max()` over promoted roles rather than a `sum()` — a `sum()` would demand a budget larger than
reality, since only one service's rollout wait is ever the binding one (k8s/rollout-drain
batches them).
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
    # revert POST, the detach POST) — see k8s/volume-revert/CLAUDE.md's sequence table. Reached
    # by slow success (or failing on the LAST wait after the rest succeeded slowly) — never by
    # failures compounding across claims, since a failure aborts the play immediately. See the
    # module docstring.
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


_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_MANIFESTS_ROLLOUT_DEFAULT_S = (
    300  # k8s/manifests: manifests_rollout_timeout | default('300s')
)


def _rollout_timeout_s(role: str) -> int:
    import re

    tasks = _K8S_ROLES / role / "tasks/main.yml"
    text = tasks.read_text() if tasks.exists() else ""
    m = re.search(r"manifests_rollout_timeout:\s*(\d+)s", text)
    return int(m.group(1)) if m else _MANIFESTS_ROLLOUT_DEFAULT_S


def _promoted_claim_roles() -> list[tuple[str, int]]:
    """(role, claim count) for every role that is BOTH auto-deployable and claim-declaring.

    These are the only roles that cost anything on the revert path, so they are the only ones
    the per-tick cap needs to bound.
    """
    out = []
    for defaults_path in sorted(_K8S_ROLES.glob("*/defaults/main.yml")):
        data = yaml.safe_load(defaults_path.read_text()) or {}
        if not data.get("k8s_autodeploy"):
            continue
        claims = data.get("k8s_autodeploy_snapshot_pvcs") or []
        if claims:
            out.append((defaults_path.parent.parent.name, len(claims)))
    return out


def test_batch_of_claim_services_fits_the_rollback_budget():
    """The multi-service axis the per-claim test above structurally cannot see (review H2).

    `deploy_k8s` joins the whole promoted set into ONE ansible-playbook run under one
    `K8S_ROLLBACK_TIMEOUT_S`, and each claim-declaring service pays its own snapshot+revert
    phase serially inside it. Only the rollout WAIT is deduped, by k8s/rollout-drain — hence
    `max()` on that term and `sum()` on the rest.

    Reverting `gitops_deploy_k8s_autodeploy_max_claim_services_per_tick` to 3 (or removing the
    cap, which reads as 0 -> treated as unbounded here) fails this test: two co-batched
    single-claim services already come to ~1680s against the 1320s budget.
    """
    revert_defaults = yaml.safe_load(_VOLUME_REVERT_DEFAULTS.read_text())
    snapshot_defaults = yaml.safe_load(
        (_K8S_ROLES / "volume-snapshot/defaults/main.yml").read_text()
    )
    gitops_defaults = yaml.safe_load(_GITOPS_DEPLOY_DEFAULTS.read_text())
    all_vars = yaml.safe_load(_ALL_VARS.read_text())

    state_timeout = int(revert_defaults["volume_revert_state_timeout"])
    api_timeout = int(revert_defaults["volume_revert_api_timeout"])
    snapshot_timeout = int(snapshot_defaults["volume_snapshot_timeout"])
    stabilise = int(all_vars["k8s_rollout_stabilise_seconds"])
    rollback_timeout = int(gitops_defaults["gitops_deploy_k8s_rollback_timeout_s"])

    cap = int(
        gitops_defaults["gitops_deploy_k8s_autodeploy_max_claim_services_per_tick"]
    )
    assert cap > 0, (
        "gitops_deploy_k8s_autodeploy_max_claim_services_per_tick must be a positive cap — 0 "
        "disables it, which restores the unbounded batch this test exists to prevent"
    )

    promoted = _promoted_claim_roles()
    assert promoted, (
        "no role is both k8s_autodeploy: true and claim-declaring; this test would pass "
        "vacuously — check the reader before trusting a green run"
    )

    # The worst batch the cap still permits: the `cap` most expensive claim-declaring services.
    per_claim = snapshot_timeout + 3 * (state_timeout + api_timeout)
    by_cost = sorted(promoted, key=lambda rc: rc[1] * per_claim, reverse=True)[:cap]
    batch_revert = sum(claims * per_claim for _, claims in by_cost)
    # Deduped across the batch by k8s/rollout-drain, so max() not sum().
    batch_rollout = max(_rollout_timeout_s(role) for role, _ in by_cost)
    worst_batch = batch_revert + batch_rollout + stabilise

    assert worst_batch <= rollback_timeout, (
        f"the worst batch the cap permits ({cap} claim-declaring service(s): "
        f"{[r for r, _ in by_cost]}) costs {worst_batch}s "
        f"(revert {batch_revert}s + rollout {batch_rollout}s + stabilise {stabilise}s), over "
        f"gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s). Past that budget run()'s "
        f"killpg fires MID-REVERT — after volume-revert scaled the workload to zero replicas "
        f"and attached its volume with disableFrontend: true. Lower "
        f"gitops_deploy_k8s_autodeploy_max_claim_services_per_tick, or raise the budget within "
        f"the unit's TimeoutStartSec."
    )
