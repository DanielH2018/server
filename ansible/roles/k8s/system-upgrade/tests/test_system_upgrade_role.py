"""The `system-upgrade` role's render: two Plans that select no node, the gate's grants, the pins (H6.1b).

Run: uv run pytest ansible/roles/k8s/system-upgrade/tests/test_system_upgrade_role.py
"""

import re

import pytest
from _k8s_render import render_role_template

from lib import yaml_fast
from lib.k8s_roles import role_fields
from lib.repo_paths import K8S_ROLES

ROLE = K8S_ROLES / "system-upgrade"
DEFAULTS = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
PLANS = ("plan-k3s-server.yaml.j2", "plan-k3s-agent.yaml.j2")
NO_NODE = "no-node-selected"
GATE_DIR = "/host/usr/lib/homelab/"


def _plan(template: str, nodes=None) -> dict:
    overrides = {} if nodes is None else {"system_upgrade_nodes": nodes}
    return yaml_fast.safe_load(
        render_role_template("system-upgrade", template, overrides)
    )


def _clause(plan: dict, key: str) -> dict:
    [clause] = [
        c for c in plan["spec"]["nodeSelector"]["matchExpressions"] if c["key"] == key
    ]
    return clause


def _rendered(name: str) -> list[dict]:
    text = render_role_template("system-upgrade", name)
    return [d for d in yaml_fast.safe_load_all(text) if d]


def _upstream(name: str) -> list[dict]:
    text = (ROLE / "files" / "upstream" / name).read_text()
    return [d for d in yaml_fast.safe_load_all(text) if d]


def _patch(name: str) -> dict:
    return yaml_fast.safe_load((ROLE / "files" / "patches" / name).read_text())


def unpinned_images(docs: list[dict]) -> list[str]:
    """Each container image in `docs` that names no `@sha256:` digest.

    Reads pod specs, and a Plan's `prepare.image`. A Plan's `upgrade.image` is outside it,
    because SUC builds that reference from the Plan's `version` (H6 *Open questions* 12).
    """
    found = []
    for doc in docs:
        spec = doc.get("spec") or {}
        pod = (spec.get("template") or {}).get("spec") or {}
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            found.append(container["image"])
        if doc.get("kind") == "Plan" and "prepare" in spec:
            found.append(spec["prepare"]["image"])
    return [image for image in found if "@sha256:" not in image]


@pytest.mark.parametrize("template", PLANS)
def test_every_plan_renders_the_pin_and_names_the_gate_account(template):
    plan = _plan(template)
    assert plan["spec"]["version"] == DEFAULTS["k3s_version"]
    assert plan["spec"]["serviceAccountName"] == "k3s-plan"
    assert plan["spec"]["concurrency"] == 1
    # No cordon and no drain: a failed Job would leave a cordoned node cordoned.
    assert not {"cordon", "drain"} & set(plan["spec"])


@pytest.mark.parametrize("template", PLANS)
def test_an_empty_node_list_selects_no_node_and_a_named_node_selects_it(template):
    assert _clause(_plan(template, []), "kubernetes.io/hostname")["values"] == [NO_NODE]
    assert _clause(_plan(template, ["daniel-box"]), "kubernetes.io/hostname")[
        "values"
    ] == ["daniel-box"]
    # H6.3c widened the list to the server node, and H6.3d to daniel-server once its package
    # shipped k3s_gates.py through the k3s-gates component (#6413).
    assert DEFAULTS["system_upgrade_nodes"] == ["daniel-box", "daniel-server"]


@pytest.mark.parametrize("template", PLANS)
def test_the_pause_label_excludes_a_node(template):
    clause = _clause(_plan(template), "homelab/k3s-upgrade-paused")
    assert clause["operator"] == "DoesNotExist"


def test_the_server_selects_the_control_plane_and_the_agent_does_not():
    server = _clause(_plan(PLANS[0]), "node-role.kubernetes.io/control-plane")
    agent = _clause(_plan(PLANS[1]), "node-role.kubernetes.io/control-plane")
    assert (server["operator"], server["values"]) == ("In", ["true"])
    assert agent["operator"] == "DoesNotExist"


@pytest.mark.parametrize("template", PLANS)
def test_prepare_mounts_nothing_and_runs_from_the_host_package(template):
    prepare = _plan(template)["spec"]["prepare"]
    # SUC already mounts the host's root at /host, so a `volumes` entry would be a second mount.
    assert "volumes" not in prepare
    assert prepare["args"][0].startswith(GATE_DIR)
    assert prepare["args"][-2:] == ["--host-root", "/host"]
    assert prepare["image"] == DEFAULTS["system_upgrade_gate_image"]


def test_the_two_plans_run_the_gate_in_their_own_mode():
    modes = [_plan(t)["spec"]["prepare"]["args"][1] for t in PLANS]
    assert modes == ["server", "agent"]


def test_the_backoff_limit_patch_equals_its_default():
    env = _patch("system-upgrade-controller-env.yaml")["data"]
    assert (
        int(env["SYSTEM_UPGRADE_JOB_BACKOFF_LIMIT"])
        == (DEFAULTS["system_upgrade_job_backoff_limit"])
    )


def test_the_agent_deadline_covers_the_server_plus_longhorns_recovery():
    server = _plan(PLANS[0])["spec"]["jobActiveDeadlineSecs"]
    agent = _plan(PLANS[1])["spec"]["jobActiveDeadlineSecs"]
    env = _patch("system-upgrade-controller-env.yaml")["data"]
    assert agent >= server + 1800
    # The controller caps a Plan's deadline at the MAX, so the MAX must admit the agent's.
    assert int(env["SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS_MAX"]) >= agent
    assert int(env["SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS"]) == server
    assert (server, agent) == (
        DEFAULTS["system_upgrade_server_deadline_secs"],
        DEFAULTS["system_upgrade_agent_deadline_secs"],
    )
    fields, problems = role_fields(["system-upgrade"])
    assert problems == []
    timeout = re.fullmatch(r"(\d+)m", fields["system-upgrade"]["timeout"])
    assert timeout is not None, "the Kustomization timeout is whole minutes"
    assert int(timeout[1]) * 60 > agent


def test_the_kustomization_fields_come_from_the_role_and_a_plain_role_has_none():
    fields, problems = role_fields(["system-upgrade", "metallb"])
    assert problems == []
    assert fields["system-upgrade"]["timeout"] == "100m"
    assert fields["system-upgrade"]["prune"] is False
    assert "health_check_exprs" in fields["system-upgrade"]
    assert "health_check_exprs" not in fields["metallb"]
    assert fields["metallb"]["prune"] is False


def test_the_gate_account_reads_what_the_gates_read_and_nothing_more():
    docs = {(d["kind"], d["metadata"]["name"]): d for d in _rendered("rbac.yaml.j2")}
    assert set(docs) == {
        ("ServiceAccount", "k3s-plan"),
        ("ClusterRole", "k3s-plan-gate"),
        ("ClusterRoleBinding", "k3s-plan-gate"),
        ("Role", "k3s-plan-kube-system"),
        ("RoleBinding", "k3s-plan-kube-system"),
    }
    cluster = docs[("ClusterRole", "k3s-plan-gate")]["rules"]
    assert {
        (g, r, tuple(rule["verbs"]))
        for rule in cluster
        for g in rule["apiGroups"]
        for r in rule["resources"]
    } == {
        ("", "nodes", ("get", "list")),
        ("longhorn.io", "volumes", ("get", "list")),
        ("longhorn.io", "backups", ("get", "list")),
        ("upgrade.cattle.io", "plans", ("get", "list")),
    }
    kube_system = docs[("Role", "k3s-plan-kube-system")]
    assert kube_system["metadata"]["namespace"] == "kube-system"
    assert {
        (rule["apiGroups"][0], rule["resources"][0], tuple(rule["resourceNames"]))
        for rule in kube_system["rules"]
    } == {
        ("apps", "deployments", ("coredns",)),
        ("coordination.k8s.io", "leases", ("kube-controller-manager",)),
    }
    assert all(rule["verbs"] == ["get"] for rule in kube_system["rules"])
    for key in (
        ("ClusterRoleBinding", "k3s-plan-gate"),
        ("RoleBinding", "k3s-plan-kube-system"),
    ):
        assert docs[key]["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "k3s-plan",
                "namespace": "system-upgrade",
            }
        ]
        assert docs[key]["roleRef"]["name"] == key[1]


def denies_all_ingress(policy: dict) -> bool:
    """Whether `policy` selects every pod, governs Ingress and allows none (ADR-0009)."""
    spec = policy.get("spec", {})
    return (
        spec.get("podSelector") == {}
        and spec.get("policyTypes") == ["Ingress"]
        and not spec.get("ingress")
    )


def test_the_namespace_denies_all_ingress():
    [policy] = _rendered("networkpolicy.yaml.j2")
    assert policy["metadata"] == {
        "name": "default-deny-ingress",
        "namespace": "system-upgrade",
    }
    assert denies_all_ingress(policy)


def test_a_policy_that_allows_ingress_or_drops_the_type_is_flagged():
    # The red fixtures: a rule that admits a peer, a policy that governs nothing, and one that
    # selects only some pods.
    [policy] = _rendered("networkpolicy.yaml.j2")
    assert not denies_all_ingress(
        {**policy, "spec": {**policy["spec"], "ingress": [{}]}}
    )
    assert not denies_all_ingress(
        {**policy, "spec": {**policy["spec"], "policyTypes": []}}
    )
    assert not denies_all_ingress(
        {
            **policy,
            "spec": {**policy["spec"], "podSelector": {"matchLabels": {"a": "b"}}},
        }
    )


def test_every_container_image_in_the_rendered_directory_carries_a_digest():
    controller = [
        d
        for d in _upstream("system-upgrade-controller.yaml")
        if d["kind"] == "Deployment"
    ]
    patched = _patch("system-upgrade-controller.yaml")
    [container] = patched["spec"]["template"]["spec"]["containers"]
    # The patch replaces the vendored Deployment's tag-only image, matched by container name.
    names = [c["name"] for c in controller[0]["spec"]["template"]["spec"]["containers"]]
    assert names == [container["name"]]
    controller[0]["spec"]["template"]["spec"]["containers"] = [container]
    docs = controller + _rendered(PLANS[0]) + _rendered(PLANS[1])
    assert unpinned_images(docs) == []


def test_a_tag_only_image_is_what_the_digest_check_flags():
    vendored = [
        d
        for d in _upstream("system-upgrade-controller.yaml")
        if d["kind"] == "Deployment"
    ]
    assert unpinned_images(vendored) == ["rancher/system-upgrade-controller:v0.20.2"]
    plan = _rendered(PLANS[0])[0]
    plan["spec"]["prepare"]["image"] = "python:3.14-alpine"
    assert unpinned_images([plan]) == ["python:3.14-alpine"]


def test_the_controller_patch_holds_the_image_pin():
    [container] = _patch("system-upgrade-controller.yaml")["spec"]["template"]["spec"][
        "containers"
    ]
    assert container["image"] == DEFAULTS["system_upgrade_controller_image"]
    # The tag is the vendored manifest's version, which a Renovate image bump alone would move.
    tag = DEFAULTS["system_upgrade_controller_image"].split("@")[0].split(":")[1]
    assert tag == DEFAULTS["system_upgrade_controller_version"]
    [vendored] = [
        d
        for d in _upstream("system-upgrade-controller.yaml")
        if d["kind"] == "Deployment"
    ]
    [upstream_container] = vendored["spec"]["template"]["spec"]["containers"]
    assert upstream_container["image"].split(":")[1] == tag


def test_the_controller_declares_no_container_port():
    # H6 Open question 13: with no port, nothing in the namespace accepts a connection, and the
    # render test of the policy is the whole proof. A release that adds a port needs a
    # netpol-baseline `refused` row with `policy: default-deny-ingress`.
    [vendored] = [
        d
        for d in _upstream("system-upgrade-controller.yaml")
        if d["kind"] == "Deployment"
    ]
    for container in vendored["spec"]["template"]["spec"]["containers"]:
        assert "ports" not in container


def test_the_vendored_namespace_is_the_one_every_template_names():
    [namespace] = [
        d
        for d in _upstream("system-upgrade-controller.yaml")
        if d["kind"] == "Namespace"
    ]
    assert namespace["metadata"]["name"] == "system-upgrade"
    for template in (*PLANS, "networkpolicy.yaml.j2"):
        assert _rendered(template)[0]["metadata"]["namespace"] == "system-upgrade"
