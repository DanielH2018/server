"""Guards on which k8s roles gitops_deploy may auto-deploy.

`roles/k8s/manifests` waits on exactly ONE deployment —
`deploy/{{ manifests_rollout | default(manifests_service) }}` — and runs `assert_stable.yml`
against that same single name. Two role shapes therefore auto-deploy without a working gate:

  * a role rendering more than one `kind: Deployment`: the extra one is applied by
    `kubectl apply -f <dir>/` but never waited on, so a bump to its image is deployed and never
    verified (prowlarr's flaresolverr and freshrss's feed-cache are the live instances);
  * a role passing `manifests_rollout: ''`, which skips the rollout wait AND the stability soak
    outright;
  * a role whose Deployment declares no `readinessProbe`: `rollout status` then returns the
    moment the pod reports Running, which proves only that the image exists.

All three are fine for a hand-deployed role — an operator is watching. None is fine for an
auto-deployed one, so the denylist must cover them. Asserting it here means a role that later
grows a second Deployment fails the suite instead of silently auto-deploying ungated.

These guards were belt-and-braces while `gitops_deploy_k8s_autodeploy_pilot` named a single
service; clearing the pilot on 2026-08-16 made them the only thing standing between a role shape
and an ungated auto-deploy. The probe guard was added in that same commit, after six services
turned out to match an existing exclusion class while sitting outside the denylist.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_K8S_ROLES = _REPO / "ansible/roles/k8s"
_DEPLOYER_DEFAULTS = _REPO / "ansible/roles/setup/gitops_deploy/defaults/main.yml"
# Not a workload role — the shared include every other role calls.
_SHARED = {"manifests", "seed-volume"}


def _denylist() -> set[str]:
    data = yaml.safe_load(_DEPLOYER_DEFAULTS.read_text())
    return set(data["gitops_deploy_k8s_autodeploy_denylist"])


def _roles() -> list[Path]:
    return sorted(
        p for p in _K8S_ROLES.iterdir() if p.is_dir() and p.name not in _SHARED
    )


def _deployment_templates(role: Path) -> list[str]:
    """Templates rendering a `kind: Deployment`, by name."""
    out = []
    for t in (
        sorted((role / "templates").glob("*.j2"))
        if (role / "templates").is_dir()
        else []
    ):
        if re.search(r"^kind:\s*Deployment\s*$", t.read_text(), re.MULTILINE):
            out.append(t.name)
    return out


def _sets_empty_rollout(role: Path) -> bool:
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return False
    return bool(
        re.search(
            r"""manifests_rollout:\s*(""|'')\s*$""", tasks.read_text(), re.MULTILINE
        )
    )


def test_auto_deployable_roles_render_exactly_one_deployment() -> None:
    denylist = _denylist()
    offenders = []
    for role in _roles():
        if role.name in denylist:
            continue
        deployments = _deployment_templates(role)
        if len(deployments) > 1:
            offenders.append(
                f"{role.name}: renders {len(deployments)} Deployments ({', '.join(deployments)}) "
                f"but k8s/manifests gates only one"
            )
    assert not offenders, (
        "Auto-deployable role(s) with an ungated Deployment — add to "
        "gitops_deploy_k8s_autodeploy_denylist with a reason, or give the role a single "
        "Deployment:\n" + "\n".join(offenders)
    )


def _has_readiness_probe(role: Path) -> bool:
    return any(
        "readinessProbe" in (role / "templates" / name).read_text()
        for name in _deployment_templates(role)
    )


def test_auto_deployable_roles_declare_a_readiness_probe() -> None:
    denylist = _denylist()
    offenders = [
        f"{role.name}: Deployment has no readinessProbe — `rollout status` returns on Running"
        for role in _roles()
        if role.name not in denylist
        and _deployment_templates(role)
        and not _has_readiness_probe(role)
    ]
    assert not offenders, (
        "Auto-deployable role(s) whose rollout gate proves nothing — add to "
        "gitops_deploy_k8s_autodeploy_denylist with a reason, or give the Deployment a "
        "readinessProbe:\n" + "\n".join(offenders)
    )


def test_auto_deployable_roles_do_not_skip_the_rollout_gate() -> None:
    denylist = _denylist()
    offenders = [
        f"{role.name}: passes manifests_rollout: '' — rollout wait and stability soak both skipped"
        for role in _roles()
        if role.name not in denylist and _sets_empty_rollout(role)
    ]
    assert not offenders, (
        "Auto-deployable role(s) with no rollout gate at all — add to "
        "gitops_deploy_k8s_autodeploy_denylist with a reason:\n" + "\n".join(offenders)
    )
