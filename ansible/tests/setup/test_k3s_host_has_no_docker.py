#!/usr/bin/env python3
"""Guards that the k3s nodes never get Docker installed on them.

One invariant, pinned at both layers: the inventory must not ask for Docker on a k3s node,
and the k3s role must refuse to install onto a host that already has it. Two incidents, one
per layer, and each is the reason the other is not enough on its own.

THE INVENTORY LAYER (daniel-box, 2026-08-01). k3s ships its own containerd plus
flannel/kube-proxy iptables rules, and daniel-box was chosen to host the cluster
first precisely because it had no container runtime — see
docs/archive/k3s-migration/slice-0-cluster-foundation.md.

A bare `initial_setup.yml` run (no --tags) then installed Docker there, because
docker_install was unconditional. That put Docker's DOCKER/DOCKER-USER chains and
FORWARD-policy handling alongside k3s networking, and tripped the k3s role's own
fail-closed guard:

    Docker is installed on daniel-box. This role targets a host with no container
    runtime; k3s brings its own containerd and its iptables rules would land [...]

which blocked k3s-bringup.yml from re-running. Docker was purged the same day.

The k3s role's guard catches this at k3s-install time, but only *after* Docker is
already on the host. `has_docker` is the half that stops it landing in the first
place — the original note said to remember `--tags`, and relying on that is exactly
what let it happen.

THE ROLE LAYER (daniel-server, 2026-08-19). That install-time guard existed in
tasks/server.yml from the start, carrying a comment that named the hazard exactly.
tasks/agent.yml listed the same assert as *deliberately* omitted, which was correct
while the Docker drain was in progress and wrong the moment it finished.

Nothing noticed the difference. daniel-server — the agent — had docker-ce purged on
2026-08-14 and reinstalled on 2026-08-19 at 22:37, then ran a second container runtime
for eight days. Every repo-side check read green throughout, because the only host the
guard covered was the one that never had Docker.

A guard on one of two symmetric paths is not a guard. The last two tests assert both
node roles carry it, so removing either one fails the suite instead of quietly halving
the coverage.

Run: uv run pytest ansible/tests/setup/test_k3s_host_has_no_docker.py
"""

import re
from pathlib import Path

import pytest
from lib import yaml_fast
from _helpers import ANSIBLE


# The k3s-bringup.yml play asserts `inventory_hostname == 'daniel-box'`, so the
# cluster *server* is a single named host rather than an inventory group today.
# daniel-server joined as an agent node on 2026-08-14, when its Docker workload
# finished draining and Docker was uninstalled — both nodes must stay Docker-free.
K3S_HOSTS = ("daniel-box", "daniel-server")

K3S_TASKS = ANSIBLE / "roles" / "setup" / "k3s" / "tasks"

# The two node roles. Named explicitly rather than globbed: a new tasks file in this role
# is not automatically a node-install path, and globbing would make this test fail for
# reasons that have nothing to do with the guard.
NODE_TASK_FILES = ["server.yml", "agent.yml"]


def _load(path: Path):
    return yaml_fast.safe_load(path.read_text())


def test_docker_install_is_gated_on_has_docker():
    """The install half must stay gated on has_docker — now inside the role.

    The gate moved on 2026-08-17. It used to be `when: has_docker` on the
    initial_setup.yml role entry, which stopped Docker landing on the k3s node but
    also skipped the role wholesale — so a host flipped to has_docker: false got no
    teardown either, and daniel-server's 2026-08-14 uninstall left an enabled
    docker-compose-qbittorrent.service and two crons for retired services behind.
    tasks/main.yml now dispatches on has_docker, so the role is included
    unconditionally and this asserts the gate at its new home.
    """
    plays = _load(ANSIBLE / "initial_setup.yml")
    roles = [r for play in plays for r in play.get("roles", [])]
    docker_entries = [
        r for r in roles if isinstance(r, dict) and r.get("role") == "docker_install"
    ]
    assert docker_entries, "docker_install is no longer wired into initial_setup.yml"
    for entry in docker_entries:
        assert "when" not in entry, (
            "docker_install must be included unconditionally — the role dispatches on "
            "has_docker internally. Re-gating it here silently disables the teardown."
        )

    tasks = _load(ANSIBLE / "roles/setup/docker_install/tasks/main.yml")
    # Static imports since #1998 (so the granular tags reach their tasks); the gate is the
    # same `when:` either way, and this guard is about the gate.
    gates = {
        t.get("ansible.builtin.import_tasks", t.get("ansible.builtin.include_tasks")): (
            t.get("when")
        )
        for t in tasks
        if "ansible.builtin.import_tasks" in t or "ansible.builtin.include_tasks" in t
    }
    assert gates.get("install.yml") == "has_docker", (
        "install.yml must run only `when: has_docker` — without it a bare "
        "initial_setup.yml run reinstalls Docker on the k3s node."
    )
    assert gates.get("teardown.yml") == "not has_docker", (
        "teardown.yml must run `when: not has_docker` — it is the only declarative "
        "reaper for units and crons left by a retired Docker plane."
    )


def test_has_docker_defaults_true_fleet_wide():
    """Existing Docker hosts must keep working without a per-host opt-in."""
    all_vars = _load(ANSIBLE / "inventory" / "group_vars" / "all.yml")
    assert all_vars.get("has_docker") is True, (
        "has_docker must default true in group_vars/all.yml — the containers_list "
        "plane depends on it."
    )


@pytest.mark.parametrize("host", K3S_HOSTS)
def test_k3s_host_opts_out_of_docker(host):
    """The k3s node must set has_docker false."""
    host_vars = _load(ANSIBLE / "inventory" / "host_vars" / f"{host}.yml")
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
    deploy on a host with no Docker. Held since the 2026-08-14 uninstall, never asserted."""
    containers_list = _load(ANSIBLE / "inventory" / "host_vars" / f"{host}.yml").get(
        "containers_list"
    )
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


@pytest.mark.parametrize("task_file", NODE_TASK_FILES)
def test_node_role_stats_the_docker_binary(task_file):
    text = (K3S_TASKS / task_file).read_text()
    assert "/usr/bin/docker" in text, (
        f"{task_file} does not stat /usr/bin/docker. Both k3s node roles must refuse to "
        f"install onto a host running Docker -- see this module's docstring for the "
        f"eight days that cost."
    )


@pytest.mark.parametrize("task_file", NODE_TASK_FILES)
def test_node_role_asserts_docker_is_absent(task_file):
    """The stat alone proves nothing -- it is the assert that fails the run."""
    text = (K3S_TASKS / task_file).read_text()
    assert re.search(r"that:\s*not \w*docker\w*\.stat\.exists", text), (
        f"{task_file} stats the Docker binary but does not assert on the result. A "
        f"registered stat with no assert reads like a guard and enforces nothing."
    )
