"""The real Pi package runs its services through the Compose project (H5.2b).

The censuses in `test_pi_compose_census.py` run over fixture services. This file runs them over
the real render of `hosts/daniel-pi/package.yml`, which runs docker-proxy (H5.6), autoheal
(H5.7), alloy (H5.8) and wg-easy (H5.9). H5.2b's `pi-canary` ran here until H5.10 removed it, and
the file keeps its name. A plain inventory variable such as `domain` renders at build time
(operator ruling 2026-10-24, h5-pi-compose.md *Open questions* 7). The package's Compose env file
holds only the keys autoheal and alloy read.

Run: uv run pytest hosts/lib/tests/test_pi_compose_canary.py
"""

from pathlib import PurePosixPath

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
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_compose import check_env_output

UNIT = "homelab-compose.service"
LINK = f"/usr/lib/systemd/system/multi-user.target.wants/{UNIT}"


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-pi")


def test_the_package_ships_the_units_enable_link_and_its_services(package):
    assert LINK in package.links
    assert f"/usr/lib/systemd/system/{UNIT}" in package.files
    services = project_of(package)["services"]
    assert "docker-proxy" in services
    assert "pi-canary" not in services


def test_the_real_project_passes_the_project_and_bind_censuses(package):
    project = project_of(package)
    listed = tmpfiles_dirs((REAL / "tmpfiles.d" / "homelab-compose.conf").read_text())
    assert project_problems(project) == []
    assert bind_problems(project, listed)[0] == []


def test_every_packaged_compose_file_asks_the_daemon_for_at_least_6_mib(package):
    """The daemon refused the canary's 2M reservation at `up` (the Pi's first run, 2026-10-24).

    The census reads every compose file the package installs and finds docker-proxy's own values,
    so a census that read no file, or no memory field, would fail here rather than pass.
    """
    project = project_of(package)
    assert memory_problems(project) == []
    proxy = project["services"]["docker-proxy"]["deploy"]["resources"]
    assert (proxy["limits"]["memory"], proxy["reservations"]["memory"]) == (
        "128M",
        "16M",
    )


def test_wg_easy_gets_the_domain_at_build_time(package):
    includes = project_of(package)["includes"]
    assert [i["path"] for i in includes] == [
        f"{COMPOSE}/alloy/compose.yaml",
        f"{COMPOSE}/autoheal/compose.yaml",
        f"{COMPOSE}/docker-proxy/compose.yaml",
        f"{COMPOSE}/wg-easy/compose.yaml",
    ]
    body = text(package, f"{COMPOSE}/wg-easy/compose.yaml")
    assert "WG_HOST=wireguard." in body
    assert "${" not in body and "{{" not in body


HOME = "/home/ubuntu"
# A data directory that a container created as root, and whose line copies the live owner (H5.9).
# Every other directory under the home is created as ubuntu.
ROOT_OWNED_DATA = frozenset({f"{HOME}/server/containers/wg-easy/config"})


def parent_problems(tmpfiles: str) -> list[str]:
    """Each `d` line under the home directory whose parents are not created as ubuntu above it.

    systemd-tmpfiles refuses a path that passes from /home/ubuntu (ubuntu) into a root-owned
    directory it created itself, so every parent between the home and the line needs its own `d`
    line owned by ubuntu, listed earlier.
    """
    created, problems = set(), []
    for line in tmpfiles.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "d" or not fields[1].startswith(f"{HOME}/"):
            continue
        path = fields[1]
        parents = [str(p) for p in PurePosixPath(path).parents if str(p) != HOME]
        parents = [p for p in parents if p.startswith(f"{HOME}/")]
        problems += [
            f"{path}: {p} is not created above it" for p in parents if p not in created
        ]
        if fields[3] != "ubuntu" and path not in ROOT_OWNED_DATA:
            problems.append(f"{path}: owner {fields[3]}, not ubuntu")
        created.add(path)
    return problems


def test_every_service_directory_line_has_its_parents_created_as_ubuntu_above_it():
    """ACCEPT the real file, and find docker-proxy's line and both parents in it."""
    lines = (REAL / "tmpfiles.d" / "homelab-compose.conf").read_text()
    assert parent_problems(lines) == []
    assert f"d {HOME}/server/containers/docker-proxy " in lines


def test_a_service_directory_line_without_its_parents_is_flagged():
    """REJECT: the shape that failed in the container case on a Pi with no /home/ubuntu/server."""
    bare = f"d {HOME}/server/containers/docker-proxy 0755 ubuntu ubuntu -\n"
    assert parent_problems(bare) == [
        f"{HOME}/server/containers/docker-proxy: {HOME}/server/containers is not created above it",
        f"{HOME}/server/containers/docker-proxy: {HOME}/server is not created above it",
    ]


def test_a_root_owned_line_is_flagged_unless_it_is_a_listed_data_directory():
    """REJECT: a service directory owned by root, which tmpfiles would take for an unsafe path."""
    parents = f"d {HOME}/server - ubuntu ubuntu -\nd {HOME}/server/containers - ubuntu ubuntu -\n"
    leaf = f"{HOME}/server/containers/wg-easy"
    root_owned = f"d {leaf}/config 0755 root root -\n"
    ubuntu_dir = f"d {leaf} 0755 ubuntu ubuntu -\n"
    assert parent_problems(parents + ubuntu_dir + root_owned) == []
    other = f"d {HOME}/server/containers/other 0755 root root -\n"
    assert parent_problems(parents + other) == [
        f"{HOME}/server/containers/other: owner root, not ubuntu"
    ]


def test_the_env_output_must_carry_exactly_the_keys_the_project_reads(package):
    """ACCEPT the keys autoheal and alloy read, REJECT a key the output does not declare."""
    keys = frozenset({"monitor_discord_webhook_url", "alloy_pi_http_password"})
    check_env_output(package, REAL, keys)
    with pytest.raises(RenderRefused, match="declares"):
        check_env_output(package, REAL, keys | {"pi_recovery_push_token"})
