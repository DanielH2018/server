"""K8S_ROLLBACK_TIMEOUT_S covers one full rollback cycle for the worst Ansible-owned service.

Split from test_gitops_deploy_timeout_budgets.py, which keeps the lock-hold and forward-cap sums
and the role readers this module shares.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_k8s_rollback_budget.py

import pathlib

import yaml
from deployer_lib.deploy_changes import ChangeSet
from deployer_lib.deploy_inventory import route_not_ansible_owned, service_owners
from lib.repo_paths import ALL_VARS, HOST_VARS

from _role_tasks import in_role_wait_s
from test_gitops_deploy_timeout_budgets import (
    _DEFAULTS,
    _K8S_ROLES_DIR,
    _rollout_timeout_s,
)

# K8S_ROLLBACK_TIMEOUT_S must cover one full rollback cycle for the most expensive promoted
# (k8s_autodeploy: true), Ansible-owned service that declares k8s_autodeploy_snapshot_pvcs: the
# pre-revert snapshot wait, the revert, the role's OWN in-role waits, the rollout wait and the
# stabilisation soak, on one timeline in one playbook run (a failure aborts the play, so it
# never compounds; docs/gitops-pipeline.md, *The rollback timeout, derived*).
#
# Deliberately a PER-SERVICE bound: co-batched claim-declaring services stack their
# snapshot+revert phases (only the rollout WAIT is deduped, via roles/k8s/manifests/tasks/drain.yml).
# That gap is recorded at the DECIDED marker on the claim cap in deploy_k8s.py and in
# defaults/main.yml, and is not modeled here. Computed from role SOURCES, so a rollout-timeout
# bump or a new claim-declaring role fails this test instead of under-sizing the budget.


# Only an Ansible-owned role counts. The filter is the deployer's own: `service_owners` reads the
# k8s host's containers_list, and `route_not_ansible_owned` moves each owned tag to `cs.flux`,
# which the tick never applies or rolls back. Every promoted, claim-declaring role is flux-owned
# since the D5 flips, so the live set is empty; `_ROLLBACK_MUST_FIND` is what shows the
# derivation still reads the tree, and the fixture pair that the deployer's filter still bites.
_ROLLBACK_MUST_FIND = "prowlarr"


def _ansible_owned(roles: set[str], owners: dict[str, str]) -> set[str]:
    cs = ChangeSet(k8s=set(roles))
    route_not_ansible_owned(cs, owners)
    return cs.k8s


def _live_owners() -> dict[str, str]:
    return service_owners((HOST_VARS / "daniel-box.yml").read_text())


def _rollback_terms() -> tuple[int, int, int]:
    """(per-claim snapshot and revert cost, stabilise soak, rollback budget), from their sources."""
    rev, snap = (
        _load(_K8S_ROLES_DIR / f"{r}/defaults/main.yml")
        for r in ("volume-revert", "volume-snapshot")
    )
    per_claim = snap["volume_snapshot_timeout"] + 3 * (
        rev["volume_revert_state_timeout"] + rev["volume_revert_api_timeout"]
    )
    stabilise = _load(ALL_VARS)["k8s_rollout_stabilise_seconds"]
    return (
        int(per_claim),
        int(stabilise),
        int(_load(_DEFAULTS)["gitops_deploy_k8s_rollback_timeout_s"]),
    )


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _worst_rollback_case(
    roles_dir, owners, per_claim, stabilise, rollout_s=None, in_role_s=None
):
    """The promoted, claim-declaring role in `roles_dir` with the longest rollback, and its length.

    A role whose tag `owners` names is skipped, as the deployer skips it. Returns `("", 0)` when no
    role qualifies, and `rollout_s`/`in_role_s` default to the readers of the real role tree.
    """
    rollout_s, in_role_s = rollout_s or _rollout_timeout_s, in_role_s or in_role_wait_s
    paths = {p.parent.parent.name: p for p in roles_dir.glob("*/defaults/main.yml")}
    worst_role, worst = "", 0
    for role in sorted(_ansible_owned(set(paths), owners)):
        role_defaults = yaml.safe_load(paths[role].read_text()) or {}
        claims = role_defaults.get("k8s_autodeploy_snapshot_pvcs") or []
        if not role_defaults.get("k8s_autodeploy") or not claims:
            continue
        ceiling = (
            len(claims) * per_claim + in_role_s(role) + rollout_s(role) + stabilise
        )
        if ceiling > worst:
            worst_role, worst = role, ceiling
    return worst_role, worst


def test_k8s_rollback_budget_covers_the_worst_single_promoted_service():
    per_claim, stabilise, rollback_timeout = _rollback_terms()
    worst_role, worst_ceiling = _worst_rollback_case(
        _K8S_ROLES_DIR, _live_owners(), per_claim, stabilise
    )
    assert worst_ceiling <= rollback_timeout, (
        f"{worst_role} needs {worst_ceiling}s for one full rollback cycle "
        f"({in_role_wait_s(worst_role)}s of in-role waits, "
        f"{_rollout_timeout_s(worst_role)}s rollout), which exceeds "
        f"gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s) — its rollback can be "
        f"SIGTERMed mid-revert. Raise that default (and TimeoutStartSec, and re-check this "
        f"test's own comment on the batch-summation gap it does not cover)."
    )


def _fixture_rollback_case(roles_dir: pathlib.Path, owners: dict[str, str]):
    # prowlarr's shape: one claim at 480s, a 780s rollout, no in-role waits and a 60s soak.
    defaults = roles_dir / "widget" / "defaults" / "main.yml"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(
        "k8s_autodeploy: true\nk8s_autodeploy_snapshot_pvcs: [widget-config]\n"
    )
    return _worst_rollback_case(roles_dir, owners, 480, 60, lambda _: 780, lambda _: 0)


def test_the_rollback_derivation_reads_the_must_find_role_once_its_owner_is_dropped():
    per_claim, stabilise, _ = _rollback_terms()
    assert _live_owners().get(_ROLLBACK_MUST_FIND) == "flux"
    found, _ = _worst_rollback_case(_K8S_ROLES_DIR, {}, per_claim, stabilise)
    assert found == _ROLLBACK_MUST_FIND, "the derivation no longer reads the role tree"


def test_an_ansible_owned_role_is_counted_in_the_rollback_budget(tmp_path):
    assert _fixture_rollback_case(tmp_path, owners={}) == ("widget", 1320)


def test_a_flux_owned_role_is_left_out_of_the_rollback_budget(tmp_path):
    assert _fixture_rollback_case(tmp_path, owners={"widget": "flux"}) == ("", 0)
    assert _fixture_rollback_case(tmp_path, owners={"widget": "released"}) == ("", 0)
