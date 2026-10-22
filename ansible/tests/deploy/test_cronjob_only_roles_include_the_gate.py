"""Every CronJob-only k8s role must gate each of its CronJobs at each revision.

A CronJob has no rollout, so nothing runs its new spec until the next scheduled firing. A
flux-owned role names each CronJob in `flux_cronjob_gates`, and the renderer copies it into a
gate Job in `<tag>-verify` that Flux runs once per role revision. A CronJob-only role without
that entry ships an image nobody has run. This test catches the missing entry before the
revision lands, the same way `test_k8s_autodeploy_batch_gates.py` catches a missing wait for a
plain Job. The `k8s/cronjob-gate` role that gated an Ansible-owned role was retired (#5716);
`docs/flux-deploy-plane.md` says what a handback restores.

Run: uv run pytest ansible/tests/deploy/test_cronjob_only_roles_include_the_gate.py
"""

from functools import cache

from _autodeploy import _K8S_ROLES
from _role_census import role_dirs
from _autodeploy_batch import _batch_gated_names
from diagnostics.probe_lib import health

_DEFAULT_NS = "homelab"

# The exact CronJob-only population (test_probe_health_resolver.py, under
# scripts/diagnostics/tests/, pins the matching set from the health-gate side). Equality rather
# than a lower bound: a THIRD role gaining a CronJob without a gate include must fail this test
# with a clear reason, not pass it silently because the assertion below only checked >= 2.
_KNOWN_CRONJOB_ONLY_ROLES = frozenset(
    {"configarr", "longhorn-reconcile", "pi-peer-backup"}
)


@cache
def _cronjob_only_census():
    """{role: [(namespace, name)]} for every k8s role declaring a CronJob and no
    Deployment/DaemonSet/StatefulSet -- the same population `role_cronjob_targets` serves at
    runtime, derived the same way (render, not a hand-written list)."""
    out = {}
    for role_dir in role_dirs():
        role = role_dir.name
        try:
            workload_targets = health.role_workload_targets(role, _DEFAULT_NS)
        except RuntimeError:
            continue
        if workload_targets is None or workload_targets:
            continue
        cronjob_targets = health.role_cronjob_targets(role, _DEFAULT_NS)
        if cronjob_targets:
            out[role] = cronjob_targets
    return out


def test_census_matches_the_known_cronjob_only_roles():
    """Non-vacuity, pinned against a concrete set rather than a lower bound -- this repo's own
    rule for a check that finds its own subject by pattern. A role silently dropping off this
    list would leave `test_every_cronjob_only_role_is_gated_at_deploy_time` below checking
    nothing for it; a role silently joining it is exactly the case that test exists to catch.
    """
    assert set(_cronjob_only_census()) == _KNOWN_CRONJOB_ONLY_ROLES


def test_every_cronjob_only_role_is_gated_at_deploy_time():
    """Each CronJob-only role names every CronJob its own manifests declare in
    `flux_cronjob_gates`, and Flux runs the gate Job in `<tag>-verify`
    (`_autodeploy_batch.flux_gated_names`). A role-local `job/<name>` wait also counts."""
    for role, targets in _cronjob_only_census().items():
        gated = _batch_gated_names(_K8S_ROLES / role)
        declared = {name for _, name in targets}
        missing = declared - gated
        assert not missing, (
            f"{role} declares CronJob(s) {sorted(missing)} with no flux_cronjob_gates entry "
            "(a flux-owned role) and no job/<name> wait in tasks/main.yml; an Ansible-owned "
            "role needs the handback steps in docs/flux-deploy-plane.md"
        )
