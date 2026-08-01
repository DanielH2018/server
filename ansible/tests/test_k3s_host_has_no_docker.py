#!/usr/bin/env python3
"""Guards that the k3s node never gets Docker installed on it.

The failure this encodes actually happened (daniel-box, 2026-08-01). k3s ships its
own containerd plus flannel/kube-proxy iptables rules, and daniel-box was chosen to
host the cluster first precisely because it had no container runtime — see
docs/k3s-migration/slice-0-cluster-foundation.md.

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

Run: uv run pytest ansible/tests/test_k3s_host_has_no_docker.py
"""

from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[1]

# The k3s-bringup.yml play asserts `inventory_hostname == 'daniel-box'`, so the
# cluster host is a single named host rather than an inventory group today.
# daniel-server joins in slice 7; add it here when its Docker workload has drained.
K3S_HOSTS = ("daniel-box",)


def _load(path: Path):
    return yaml.safe_load(path.read_text())


def test_docker_install_is_gated_on_has_docker():
    """initial_setup.yml must not run docker_install unconditionally."""
    plays = _load(ANSIBLE / "initial_setup.yml")
    roles = [r for play in plays for r in play.get("roles", [])]
    docker_entries = [
        r for r in roles if isinstance(r, dict) and r.get("role") == "docker_install"
    ]
    assert docker_entries, "docker_install is no longer wired into initial_setup.yml"
    for entry in docker_entries:
        assert entry.get("when") == "has_docker", (
            "docker_install must carry `when: has_docker` — without it a bare "
            "initial_setup.yml run reinstalls Docker on the k3s node."
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
