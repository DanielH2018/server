#!/usr/bin/env python3
"""Tests for gen_infra_map.load_roles: which role owns a container, and which roles run no workload.

Split from test_gen_infra_map.py when the Pi package's service trees joined the scan (H5.2b).

Run: uv run pytest scripts/infra_map/tests/test_infra_map_roles.py
"""

import pytest

import gen_infra_map as g


def test_load_roles_derives_ownership_from_the_role_trees():
    """The whole companion/job distinction rests on this being repo-derived."""
    roles = g.load_roles()
    if not roles.container_owners:
        pytest.skip("role trees not present")
    # autoheal is an un-inventoried companion.
    assert roles.container_owners.get("autoheal") == "autoheal"
    # A Pi package service directory, in the second tree, owns a container it does not name.
    assert roles.container_owners.get("docker-proxy-lifecycle") == "docker-proxy"
    # Both are CronJob-only k8s roles; the classification is derived from the k8s manifests.
    assert "configarr" in roles.batch_roles
    assert "pi-peer-backup" in roles.batch_roles
    # Roles whose templates hold no long-running workload at all: a route onto a
    # chart-owned Deployment, a PVC, NetworkPolicies. None may read "Missing · no deployment
    # found".
    for role in ("longhorn-ui", "media-volume", "netpol-baseline"):
        assert role in roles.batch_roles, role
    # And a DaemonSet-only role is a real workload the collector must find.
    assert "node-exporter" not in roles.batch_roles
    assert "dri-device-plugin" not in roles.batch_roles
    assert roles.manifest_namespaces["dri-device-plugin"] == {"kube-system"}
    # A role whose namespace is the resolved variable records nothing here.
    assert "freshrss" not in roles.manifest_namespaces


def _k8s_role(repo_root, name, *manifests):
    templates = repo_root / "ansible" / "roles" / "k8s" / name / "templates"
    templates.mkdir(parents=True)
    for index, kind in enumerate(manifests):
        (templates / f"{index}.yaml.j2").write_text(f"kind: {kind}\n")


def test_load_roles_reads_a_packaged_service_and_a_sub_container(tmp_path):
    """ACCEPT: a service tree owns every container_name its compose file declares, so a moved
    sub-container keeps its owner. REJECT: a role-tree-only scan has no owner for either.
    """
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    tree = (
        tmp_path / "hosts" / "components" / "pi-compose" / "services" / "docker-proxy"
    )
    tree.mkdir(parents=True)
    (tree / "compose.yaml.j2").write_text(
        "services:\n  docker-proxy:\n    container_name: docker-proxy\n"
        "  docker-proxy-lifecycle:\n    container_name: docker-proxy-lifecycle\n"
    )
    owners = g.load_roles(tmp_path).container_owners
    assert owners == {
        "docker-proxy": "docker-proxy",
        "docker-proxy-lifecycle": "docker-proxy",
    }


def test_load_roles_excuses_a_role_with_no_long_running_workload(tmp_path):
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    _k8s_role(tmp_path, "policies", "NetworkPolicy", "Job")
    _k8s_role(tmp_path, "route-only", "IngressRoute", "Middleware")
    roles = g.load_roles(tmp_path)
    assert roles.batch_roles == frozenset({"policies", "route-only"})


def test_load_roles_does_not_excuse_a_daemonset_or_statefulset_role(tmp_path):
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    _k8s_role(tmp_path, "agent", "DaemonSet", "NetworkPolicy")
    _k8s_role(tmp_path, "db", "StatefulSet")
    _k8s_role(tmp_path, "web", "Deployment", "CronJob")
    assert g.load_roles(tmp_path).batch_roles == frozenset()


def test_load_roles_records_only_literal_manifest_namespaces(tmp_path):
    (tmp_path / "ansible" / "roles" / "containers").mkdir(parents=True)
    templates = tmp_path / "ansible" / "roles" / "k8s" / "plugin" / "templates"
    templates.mkdir(parents=True)
    (templates / "a.yaml.j2").write_text(
        "kind: DaemonSet\nmetadata:\n  namespace: kube-system\n"
    )
    (templates / "b.yaml.j2").write_text(
        "kind: Secret\nmetadata:\n  namespace: {{ k8s_namespace }}\n"
    )
    roles = g.load_roles(tmp_path)
    assert roles.manifest_namespaces == {"plugin": frozenset({"kube-system"})}
