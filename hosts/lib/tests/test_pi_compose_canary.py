"""The real Pi package runs the canary through the Compose project (H5.2b).

The censuses in `test_pi_compose_census.py` run over fixture services. This file runs them over
the real render of `hosts/daniel-pi/package.yml`, whose only service is `pi-canary`: the first
service the package runs. The canary reads no secret, so the package declares no `compose.env`
output and the include names no `env_file:` (operator ruling 2026-10-24, h5-pi-compose.md
*Open questions* 7).

Run: uv run pytest hosts/lib/tests/test_pi_compose_canary.py
"""

from pathlib import PurePosixPath

import pytest
from _compose_tree import (
    COMPOSE,
    REAL,
    bind_problems,
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


def test_the_package_ships_the_units_enable_link_and_the_canary(package):
    assert LINK in package.links
    assert f"/usr/lib/systemd/system/{UNIT}" in package.files
    assert "pi-canary" in project_of(package)["services"]


def test_the_real_project_passes_the_project_and_bind_censuses(package):
    project = project_of(package)
    listed = tmpfiles_dirs((REAL / "tmpfiles.d" / "homelab-compose.conf").read_text())
    assert project_problems(project) == []
    assert bind_problems(project, listed)[0] == []


def test_the_canary_reads_no_secret_and_gets_the_domain_at_build_time(package):
    includes = project_of(package)["includes"]
    assert [i["path"] for i in includes] == [f"{COMPOSE}/pi-canary/compose.yaml"]
    assert not any("env_file" in i for i in includes)
    assert not [o for o in package.outputs if o["destination"].endswith("compose.env")]
    body = text(package, f"{COMPOSE}/pi-canary/compose.yaml")
    assert "HOMELAB_DOMAIN=" in body
    assert "${" not in body and "{{" not in body


HOME = "/home/ubuntu"


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
        if fields[3] != "ubuntu":
            problems.append(f"{path}: owner {fields[3]}, not ubuntu")
        created.add(path)
    return problems


def test_every_service_directory_line_has_its_parents_created_as_ubuntu_above_it():
    """ACCEPT the real file, and find the canary's line and both parents in it."""
    lines = (REAL / "tmpfiles.d" / "homelab-compose.conf").read_text()
    assert parent_problems(lines) == []
    assert f"d {HOME}/server/containers/pi-canary " in lines


def test_a_service_directory_line_without_its_parents_is_flagged():
    """REJECT: the shape that failed in the container case on a Pi with no /home/ubuntu/server."""
    bare = f"d {HOME}/server/containers/pi-canary 0755 ubuntu ubuntu -\n"
    assert parent_problems(bare) == [
        f"{HOME}/server/containers/pi-canary: {HOME}/server/containers is not created above it",
        f"{HOME}/server/containers/pi-canary: {HOME}/server is not created above it",
    ]


def test_a_canary_with_a_placeholder_would_need_the_env_output(package):
    """REJECT: the censuses reject a project that reads `${key}` with no matching output."""

    with pytest.raises(RenderRefused, match="declares"):
        check_env_output(package, REAL, frozenset({"monitor_discord_webhook_url"}))
