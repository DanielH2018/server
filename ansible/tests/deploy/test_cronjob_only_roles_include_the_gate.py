"""Every CronJob-only k8s role must include `k8s/cronjob-gate`, named for its own CronJob.

`probe.py health <tag>` gates a CronJob-only role by reading its most recent Job
(`scripts/diagnostics/probe_lib/health.py`'s `format_cronjob_health`) -- and that Job only
exists to read because `k8s/cronjob-gate` created one at deploy time. A CronJob-only role that
skipped that include would have nothing for the read-only gate to find: `role_cronjob_targets`
still reports it gated (the manifests declare a CronJob), `format_cronjob_health` prints "no
evidence it has ever run", and the notifier correctly fails it -- but only after a deploy
nobody meant to leave unverified. This test catches the missing include before that deploy,
the same way `test_k8s_autodeploy_batch_gates.py` catches a missing wait for a plain Job.

Run: uv run pytest ansible/tests/deploy/test_cronjob_only_roles_include_the_gate.py
"""

from __future__ import annotations

import sys

from _autodeploy import _K8S_ROLES
from _autodeploy_batch import _batch_gated_names
from _helpers import REPO

# `probe_lib.health` lives under scripts/, reached by package name -- a directly-invoked
# module gets only its own directory on sys.path, and pyproject's `pythonpath` covers
# `ansible/tests`, not `scripts`. Mirrors the insert `_k8s_render.py` and `health.py` itself use.
sys.path.insert(0, str(REPO / "scripts"))

from diagnostics.probe_lib import health

_DEFAULT_NS = "homelab"

# The exact CronJob-only population today (scripts/diagnostics/tests/test_probe_health.py pins
# the matching set from the health-gate side). Equality rather than a lower bound: a THIRD role
# gaining a CronJob without a gate include must fail this test with a clear reason, not pass it
# silently because the assertion below only checked >= 2.
_KNOWN_CRONJOB_ONLY_ROLES = frozenset({"configarr", "pi-peer-backup"})


def _cronjob_only_census():
    """{role: [(namespace, name)]} for every k8s role declaring a CronJob and no
    Deployment/DaemonSet/StatefulSet -- the same population `role_cronjob_targets` serves at
    runtime, derived the same way (render, not a hand-written list)."""
    out = {}
    for role_dir in sorted(d for d in _K8S_ROLES.iterdir() if d.is_dir()):
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
    """Each CronJob-only role's tasks/main.yml must include k8s/cronjob-gate, named for every
    CronJob its own manifests declare -- the deploy-time proof `format_cronjob_health`'s
    read-only check is verifying after the fact."""
    for role, targets in _cronjob_only_census().items():
        gated = _batch_gated_names(_K8S_ROLES / role)
        declared = {name for _, name in targets}
        missing = declared - gated
        assert not missing, (
            f"{role} declares CronJob(s) {sorted(missing)} with no matching "
            "k8s/cronjob-gate include (cronjob_gate_name) or job/<name> wait in tasks/main.yml"
        )
