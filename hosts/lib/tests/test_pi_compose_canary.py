"""The real Pi package runs the canary through the Compose project (H5.2b).

The censuses in `test_pi_compose_census.py` run over fixture services. This file runs them over
the real render of `hosts/daniel-pi/package.yml`, whose only service is `pi-canary`: the first
service the package runs. The canary reads no secret, so the package declares no `compose.env`
output and the include names no `env_file:` (operator ruling 2026-10-24, h5-pi-compose.md
*Open questions* 7).

Run: uv run pytest hosts/lib/tests/test_pi_compose_canary.py
"""

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


def test_a_canary_with_a_placeholder_would_need_the_env_output(package):
    """REJECT: the censuses reject a project that reads `${key}` with no matching output."""

    with pytest.raises(RenderRefused, match="declares"):
        check_env_output(package, REAL, frozenset({"monitor_discord_webhook_url"}))
