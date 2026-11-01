"""docker-proxy runs through the Pi's Compose project (H5.6).

The censuses in `test_pi_compose_census.py` run over fixture services. This file runs them over
the real render of `hosts/daniel-pi/package.yml` and checks the one name contract the move
carries: the sub-proxies the service file declares are the ones the recovery heartbeat adds to
its watch set whenever `docker-proxy` is a Pi service.

Run: uv run pytest hosts/lib/tests/test_pi_compose_docker_proxy.py
"""

import re

import pytest
from _compose_tree import (
    COMPOSE,
    REAL,
    bind_problems,
    memory_problems,
    project_of,
    project_problems,
    text,
    tmpfiles_dirs,
)

from renderer.renderer_lib import host as host_mode

SUB_PROXIES = {"docker-proxy", "docker-proxy-lifecycle"}


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-pi")


def watch_set(package) -> set[str]:
    """The `CONTAINERS=(...)` names of the rendered recovery heartbeat."""
    (dst,) = [d for d in package.files if d.endswith("/pi-recovery-health")]
    line = re.search(r"^CONTAINERS=\((.*)\)$", text(package, dst), re.M)
    assert line, f"{dst} carries no CONTAINERS=( line"
    return set(line.group(1).split())


def test_the_service_file_declares_the_proxy_and_its_lifecycle_sub_proxy(package):
    """Named members: has_code_server is false on the Pi, so no codeserver sub-proxy renders."""
    project = project_of(package)
    assert f"{COMPOSE}/docker-proxy/compose.yaml" in project["files"]
    declared = set(project["files"][f"{COMPOSE}/docker-proxy/compose.yaml"]["services"])
    assert declared == SUB_PROXIES


def test_the_recovery_heartbeat_watches_every_service_the_file_declares(package):
    """ACCEPT: the watch set holds both names, so a dead sub-proxy gets a `docker start`."""
    assert SUB_PROXIES <= watch_set(package)


def test_the_real_project_passes_every_census_with_docker_proxy_in_it(package):
    project = project_of(package)
    listed = tmpfiles_dirs((REAL / "tmpfiles.d" / "homelab-compose.conf").read_text())
    assert project_problems(project) == []
    assert memory_problems(project) == []
    assert bind_problems(project, listed)[0] == []
    assert "/home/ubuntu/server/containers/docker-proxy" in listed
    assert all("env_file" in i for i in project["includes"])


def test_the_proxies_join_the_networks_the_ansible_era_containers_use(package):
    """homelab-compose.service creates both external networks, and alloy and autoheal join them."""
    body = project_of(package)["files"][f"{COMPOSE}/docker-proxy/compose.yaml"]
    assert body["services"]["docker-proxy"]["networks"] == ["proxy"]
    assert body["services"]["docker-proxy-lifecycle"]["networks"] == ["lifecycle"]
    assert body["networks"] == {
        "proxy": {"external": True},
        "lifecycle": {"external": True},
    }


def test_the_proxy_alloys_log_source_reads_through_allows_container_logs(package):
    """The socket-proxy denies `/containers/<id>/logs` unless ALLOW_LOGS is set, whatever CONTAINERS is.

    Without the flag, discovery (`/containers/json`) still answers 200 while every log stream gets
    a 403, so `{job="pi"}` falls silent and `LokiPiLogsSilent` fires. The service is derived from
    the host Alloy's `loki.source.docker` dials, so a re-pointed source is checked too.
    """
    config = text(package, f"{COMPOSE}/alloy/config.alloy")
    source = re.search(r'loki\.source\.docker "pi" \{(.*?)\n\}', config, re.S)
    assert source, 'alloy\'s config carries no loki.source.docker "pi" block'
    host = re.search(r'host\s*=\s*"tcp://([\w-]+):2375"', source.group(1))
    assert host, 'loki.source.docker "pi" names no tcp://<service>:2375 host'
    reader_proxy = host.group(1)
    assert reader_proxy in SUB_PROXIES
    env = project_of(package)["services"][reader_proxy]["environment"]
    assert "ALLOW_LOGS=1" in env
