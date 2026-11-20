"""Is a Job or CronJob credited as gated -- the batch half of the auto-deploy derivation.

A batch workload is credited when its role names it in `flux_cronjob_gates`, which
the renderer turns into a gate Job Flux runs once per role revision.
Consumed by `test_k8s_autodeploy_batch_gates.py` and by `_autodeploy_rollout.py`.
"""

from pathlib import Path

from lib.k8s_roles import role_defaults


def flux_gated_names(role: Path) -> set[str]:
    """The CronJobs the role directory `role` gates through `flux_cronjob_gates`.

    A CronJob has no rollout and nothing runs it at deploy time, so this is the only credit a
    CronJob gets. The renderer copies each CronJob the role names there into a gate Job in
    `<tag>-verify`, which Flux runs once per role revision
    (`scripts/renderer/renderer_lib/cronjob_gate.py`).
    """
    found, _problems = role_defaults([role.name], roles=role.parent)
    own = found.get(role.name)
    return set(own.cronjob_gates) if own else set()
