"""What the longhorn-reconcile role renders, held to what the script and the old tasks did.

The script's behaviour is in test_longhorn_reconcile.py. These cases read the render: the
ConfigMap's tier lists against the keys `setup/k3s` still derives, the CSI request the CronJob
passes against the role's defaults, and the sidecar patch against the rule the manager's patch
meets (a memory request and no limit).

Run: uv run pytest ansible/roles/k8s/longhorn-reconcile/tests/test_longhorn_reconcile_render.py
"""

import json

import longhorn_reconcile as lr
import pytest
from _helpers import load_defaults
from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import K3S_ROLE, K8S_ROLES
from lib.service_tiers import resolved_tier_lists

ROLE = K8S_ROLES / "longhorn-reconcile"
DEFAULTS = load_defaults(ROLE)


def _docs(template: str) -> list[dict]:
    return list(
        yaml_fast.safe_load_all(render_role_template("longhorn-reconcile", template))
    )


def _configmap() -> dict:
    (doc,) = _docs("configmap.yaml.j2")
    return doc


def test_the_configmap_tiers_equal_the_three_lists_setup_k3s_derives():
    """Until H1.7a moves the keys, both forms must render the same lists (the plan's claim)."""
    tiers = json.loads(_configmap()["data"]["tiers.json"])
    keys = resolved_tier_lists(load_defaults(K3S_ROLE))
    assert tiers["r2"] == keys["k3s_longhorn_r2_volumes"]
    assert tiers["weekly"] == keys["k3s_longhorn_weekly_volumes"]
    assert tiers["nobackup"] == keys["k3s_longhorn_nobackup_volumes"]
    # Non-vacuity: each list has members, so an equality of two empty lists cannot pass.
    assert tiers["r2"] and tiers["weekly"] and tiers["nobackup"]
    assert "homelab/traefik-acme" in tiers["r2"]


def test_the_rendered_tiers_pass_the_scripts_own_validation(tmp_path):
    path = tmp_path / "tiers.json"
    path.write_text(_configmap()["data"]["tiers.json"])
    tiers = lr.load_tiers(path)
    assert set(tiers["weekly"]).isdisjoint(tiers["nobackup"])


def test_the_configmap_ships_the_script_and_the_connection_module_byte_for_byte():
    data = _configmap()["data"]
    assert (
        data["longhorn_reconcile.py"]
        == (ROLE / "files/longhorn_reconcile.py").read_text()
    )
    assert (
        data["flux_job_connect.py"]
        == (K8S_ROLES / "manifests/files/flux_job_connect.py").read_text()
    )


def _cronjob() -> dict:
    (doc,) = _docs("cronjob.yaml.j2")
    return doc


def _env() -> dict[str, str]:
    container = _cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    return {e["name"]: e["value"] for e in container["env"]}


def test_the_cronjob_passes_the_csi_request_of_the_role_defaults():
    assert (
        _env()["RECONCILE_CSI_MEMORY_REQUEST"]
        == DEFAULTS["k3s_longhorn_csi_sidecar_memory_request"]
    )
    assert _env()["RECONCILE_NAMESPACE"] == "longhorn-system"


def test_the_cronjob_runs_hourly_and_never_overlaps_itself():
    spec = _cronjob()["spec"]
    assert spec["schedule"] == "17 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "longhorn-reconcile"
    assert pod["securityContext"]["runAsNonRoot"] is True


@pytest.mark.parametrize("sidecar", lr.CSI_DEPLOYMENTS)
def test_the_sidecar_patch_sets_a_request_and_no_limit(sidecar):
    patch = lr.sidecar_patch(
        sidecar, DEFAULTS["k3s_longhorn_csi_sidecar_memory_request"]
    )
    (container,) = patch["spec"]["template"]["spec"]["containers"]
    assert container["name"] == sidecar
    assert container["resources"]["requests"]["memory"] == "192Mi"
    # A limit set too low turns an OOM-by-badness kill into an OOM-by-cgroup-cap kill (#1243).
    assert "limits" not in container["resources"]


def test_the_script_names_the_four_sidecars_the_driver_deployer_creates():
    assert set(lr.CSI_DEPLOYMENTS) == {
        "csi-attacher",
        "csi-provisioner",
        "csi-resizer",
        "csi-snapshotter",
    }
    # The Role names the same four, so the script cannot patch a Deployment the grant omits.
    rules = [
        r for d in _docs("rbac.yaml.j2") if d["kind"] == "Role" for r in d["rules"]
    ]
    (deployments,) = [r for r in rules if r["resources"] == ["deployments"]]
    assert set(deployments["resourceNames"]) == set(lr.CSI_DEPLOYMENTS)


def test_the_role_is_a_cronjob_only_role_that_the_gate_names():
    gates = DEFAULTS["flux_cronjob_gates"]
    assert gates["longhorn-reconcile"]["kind"] == "mutation"
    node = gates["longhorn-reconcile"]["rerun_safe"]
    path, test = node.split("::")
    assert test in (ROLE.parents[3] / path).read_text()
