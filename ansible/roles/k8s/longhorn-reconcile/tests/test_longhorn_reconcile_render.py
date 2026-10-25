"""What the longhorn-reconcile role renders, held to what the script and the old tasks did.

The script's behaviour is in test_longhorn_reconcile.py. These cases read the render: the
ConfigMap's tier lists against the keys `setup/k3s` still derives, and the CronJob and its grants
against the CSI sidecars, which the `longhorn` role's Setting owns (#5958).

Run: uv run pytest ansible/roles/k8s/longhorn-reconcile/tests/test_longhorn_reconcile_render.py
"""

import json

import longhorn_reconcile as lr
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


def test_the_configmap_tiers_are_the_three_lists_of_the_role_defaults():
    """The ConfigMap renders the role's three keys, and Python readers derive the same lists."""
    tiers = json.loads(_configmap()["data"]["tiers.json"])
    keys = resolved_tier_lists(DEFAULTS)
    assert tiers["r2"] == keys["k3s_longhorn_r2_volumes"]
    assert tiers["weekly"] == keys["k3s_longhorn_weekly_volumes"]
    assert tiers["nobackup"] == keys["k3s_longhorn_nobackup_volumes"]
    # Non-vacuity: each list has members, so an equality of two empty lists cannot pass.
    assert tiers["r2"] and tiers["weekly"] and tiers["nobackup"]
    assert "homelab/traefik-acme" in tiers["r2"]


def test_setup_k3s_no_longer_defines_the_tier_lists():
    """One home per key: a copy left in `setup/k3s` would drift from the ConfigMap's."""
    host_plane = load_defaults(K3S_ROLE)
    for key in (
        "k3s_longhorn_r2_volumes",
        "k3s_longhorn_weekly_volumes",
        "k3s_longhorn_nobackup_volumes",
    ):
        assert key in DEFAULTS
        assert key not in host_plane


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


def test_the_cronjob_passes_the_namespace_and_no_csi_request():
    env = _env()
    assert env["RECONCILE_NAMESPACE"] == "longhorn-system"
    assert [name for name in env if "CSI" in name] == []
    assert "k3s_longhorn_csi_sidecar_memory_request" not in DEFAULTS


def test_the_cronjob_runs_hourly_and_never_overlaps_itself():
    spec = _cronjob()["spec"]
    assert spec["schedule"] == "17 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "longhorn-reconcile"
    assert pod["securityContext"]["runAsNonRoot"] is True


def test_the_role_grants_nothing_on_deployments():
    # fact: ansible/roles/k8s/longhorn-reconcile/CLAUDE.md#Traps
    # A grant the script no longer needs is a write path to the CSI Deployments that
    # longhorn-manager undoes on its next tick, the flip-flop #5958 removed.
    rules = [
        r for d in _docs("rbac.yaml.j2") if d["kind"] == "Role" for r in d["rules"]
    ]
    assert {r["resources"][0] for r in rules} == {"volumes"}


def test_the_role_is_a_cronjob_only_role_that_the_gate_names():
    gates = DEFAULTS["flux_cronjob_gates"]
    assert gates["longhorn-reconcile"]["kind"] == "mutation"
    node = gates["longhorn-reconcile"]["rerun_safe"]
    path, test = node.split("::")
    assert test in (ROLE.parents[3] / path).read_text()
