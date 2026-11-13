"""Is a Job or CronJob credited as gated -- the batch half of the auto-deploy derivation.

A batch workload is credited when its flux-owned role names it in `flux_cronjob_gates`, which
the renderer turns into a gate Job Flux runs once per role revision.
Consumed by `test_k8s_autodeploy_batch_gates.py` and by `_autodeploy_rollout.py`.
"""

from functools import cache
from pathlib import Path

from lib.k8s_roles import k8s_tags
from renderer.renderer_lib.ordering import cronjob_gates


@cache
def _owners() -> dict[str, str]:
    return dict.fromkeys(k8s_tags(), "flux")


def flux_gated_names(role: Path, owners: dict[str, str] | None = None) -> set[str]:
    """The CronJobs a flux-owned role gates through `flux_cronjob_gates`.

    A CronJob has no rollout and nothing runs it at deploy time, so this is the only credit a
    CronJob gets. The renderer copies each CronJob the role names there into a gate Job in
    `<tag>-verify`, which Flux runs once per role revision
    (`scripts/renderer/renderer_lib/cronjob_gate.py`).
    A role Ansible still owns gets no credit for the opt-in, because nothing renders its gate.

    Args:
        role: The role directory.
        owners: Each tag's owner, `flux` for every live k8s tag when None.
    """
    if (owners if owners is not None else _owners()).get(role.name) != "flux":
        return set()
    found, _problems = cronjob_gates([role.name], roles=role.parent)
    return set(found.get(role.name, {}))
