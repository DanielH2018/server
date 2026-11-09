#!/usr/bin/env python3
"""Guards that the k3s nodes never get Docker installed on them.

One invariant, pinned at both layers: the inventory must not ask for Docker on a k3s node,
and hosts/bootstrap-host.sh must refuse to install k3s onto a host that already has it. Each
layer is the reason the other is not enough on its own.

THE INVENTORY LAYER. k3s ships its own containerd plus flannel/kube-proxy iptables rules,
and daniel-box was chosen to host the cluster first precisely because it had no container
runtime — see docs/archive/k3s-migration/slice-0-cluster-foundation.md.

A bare `initial_setup.yml` run (no --tags) installs Docker on a host where docker_install
is unconditional. That puts Docker's DOCKER/DOCKER-USER chains and FORWARD-policy handling
alongside k3s networking, and trips the install-time refusal.

That refusal catches this at k3s-install time, but only *after* Docker is already on the
host. `has_docker` is the half that stops it landing in the first
place; relying on a remembered `--tags` is what lets it happen.

THE INSTALL LAYER. hosts/bootstrap-host.sh refuses to install k3s on a host running Docker,
server and agent alike, and hosts/lib/tests/test_bootstrap_host_k3s.py holds that half. A node
without it can run a second container runtime next to k3s while every repo-side check reads
green.

Run: uv run pytest ansible/tests/setup/test_k3s_host_has_no_docker.py
"""

from pathlib import Path

import pytest
from lib import yaml_fast
from _helpers import ANSIBLE
from lib.repo_paths import ALL_VARS, HOST_VARS


# The k3s-bringup.yml play asserts `inventory_hostname == 'daniel-box'`, so the
# cluster *server* is a single named host rather than an inventory group.
# daniel-server is an agent node with Docker uninstalled — both nodes must stay Docker-free.
K3S_HOSTS = ("daniel-box", "daniel-server")


def _load(path: Path):
    return yaml_fast.safe_load(path.read_text())


def test_docker_install_is_an_end_role_stub():
    """No Ansible task touches Docker on any host.

    The Pi's package installs the engine (H7.26), and the cluster nodes' packages retire the
    teardown's crons and drop-ins as legacy entries (H7.48). The role stays an unconditional
    `end_role` stub until H7.20a deletes it with its initial_setup.yml entry.
    """
    plays = _load(ANSIBLE / "initial_setup.yml")
    roles = [r for play in plays for r in play.get("roles", [])]
    docker_entries = [
        r for r in roles if isinstance(r, dict) and r.get("role") == "docker_install"
    ]
    assert docker_entries, "docker_install is no longer wired into initial_setup.yml"
    for entry in docker_entries:
        assert "when" not in entry, (
            "docker_install must be included unconditionally, so its end_role stub runs on "
            "every host and setup_gates.role_end_guard counts the role empty everywhere."
        )

    tasks = _load(ANSIBLE / "roles/setup/docker_install/tasks/main.yml")
    assert tasks == [
        {
            "name": "End the role, whose work the host packages own",
            "ansible.builtin.meta": "end_role",
        }
    ], (
        "docker_install runs tasks again; only the host packages may install or reap Docker"
    )
    assert not (ANSIBLE / "roles/setup/docker_install/tasks/teardown.yml").exists()


def test_has_docker_defaults_false_fleet_wide():
    """A host nobody thought about must inherit no engine.

    A default of true would make the inventory layer described above depend on someone
    remembering the override. The teardown arm the false default selects is a no-op on a host
    that never had Docker.
    """
    all_vars = _load(ALL_VARS)
    assert all_vars.get("has_docker") is False, (
        "has_docker must default false in group_vars/all.yml — daniel-pi is the only "
        "Docker host, and it opts in."
    )


def test_daniel_pi_opts_in_to_docker():
    """The one host the containers_list plane still runs on says so itself."""
    host_vars = _load(HOST_VARS / "daniel-pi.yml")
    assert host_vars.get("has_docker") is True, (
        "daniel-pi runs every remaining Compose service and must set `has_docker: true` "
        "now that the fleet default is false."
    )


@pytest.mark.parametrize("host", K3S_HOSTS)
def test_k3s_host_opts_out_of_docker(host):
    """The k3s node must set has_docker false."""
    host_vars = _load(HOST_VARS / f"{host}.yml")
    assert host_vars.get("has_docker") is False, (
        f"{host} runs k3s and must set `has_docker: false`. k3s brings its own "
        "containerd; Docker's iptables rules must not land alongside it."
    )


def _docker_entries(containers_list) -> list[str]:
    """The names of the entries a Docker play would deploy: `platform: docker`, or no
    platform at all, since the deploy play's platform filter treats an absent key as Docker."""
    return [
        str(entry.get("name"))
        for entry in containers_list or []
        if isinstance(entry, dict) and entry.get("platform", "docker") != "k8s"
    ]


@pytest.mark.parametrize("host", K3S_HOSTS)
def test_k3s_host_declares_no_docker_service(host):
    """A `platform: docker` entry on a k3s node names a service the Docker play would try to
    deploy on a host with no Docker."""
    containers_list = _load(HOST_VARS / f"{host}.yml").get("containers_list")
    assert _docker_entries(containers_list) == [], (
        f"{host} has no Docker; move these entries to daniel-pi or to platform: k8s"
    )


def test_docker_entry_detector_flags_a_docker_platform_and_an_absent_one():
    """Red-proof: the two shapes the deploy play sends to the Docker plane are both caught."""
    assert _docker_entries(
        [
            {"name": "a", "platform": "docker"},
            {"name": "b"},
            {"name": "c", "platform": "k8s"},
        ]
    ) == ["a", "b"]
