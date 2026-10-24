"""daniel-server's `nut-secondary` component: the rendered `upsmon.conf` that nothing reads (H4.3a).

H4.3a ships one declared output and no consumer. These tests hold what the PR adds: the output's
mode, owner and `persist` flag, the absence of a symlink onto the nut-client conffile, the
upsd endpoint and the placeholder in the MONITOR line, and the `role:` key that lets a component
render the `nut_host` role's template and defaults in place.

Run: uv run pytest hosts/lib/tests/test_nut_secondary_component.py
"""

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused

COMPONENT = REPO / "hosts" / "components" / "nut-secondary" / "component.yml"
TEMPLATE = "/usr/lib/homelab/secret-templates/etc/nut/upsmon.conf"
CONF = "/etc/nut/upsmon.conf"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-server")


def test_the_output_is_persisted_root_nut_0640_with_no_dependent(package):
    [row] = [o for o in package.outputs if o["destination"] == "etc/nut/upsmon.conf"]
    assert (row["owner"], row["group"], row["mode"]) == ("root", "nut", "0640")
    assert row["persist"] is True
    assert row["dependents"] == []
    assert row["keys"] == ["nut_monitor_password"]
    assert row["template"] == TEMPLATE


def test_no_symlink_lands_on_the_nut_client_conffile(package):
    # A link at /etc/nut/upsmon.conf would make nut-monitor read the file H4.3a leaves unread.
    assert CONF not in package.links
    assert CONF not in package.files


def test_the_monitor_line_names_the_loopback_upsd_and_a_placeholder(package):
    # R10-01: without `inventory_hostname` this renders `apc-ups@` with an empty host.
    lines = package.files[TEMPLATE][0].decode().splitlines()
    [monitor] = [line for line in lines if line.startswith("MONITOR ")]
    assert monitor.split()[1] == "apc-ups@127.0.0.1"
    assert "${nut_monitor_password}" in monitor


def test_the_package_lists_the_component():
    spec = yaml.safe_load((REPO / "hosts/daniel-server/package.yml").read_text())
    assert "nut-secondary" in spec["components"]
    assert yaml.safe_load(COMPONENT.read_text())["role"] == "nut_host"


def render_with(tmp_path, component: dict):
    """daniel-server rendered from a fixture `hosts/` tree holding only `component`."""
    root = tmp_path / "hosts"
    (root / "lib").mkdir(parents=True)
    (root / "components" / "fixture").mkdir(parents=True)
    (root / "components" / "fixture" / "component.yml").write_text(
        yaml.safe_dump(component)
    )
    (root / "daniel-server").mkdir()
    (root / "daniel-server" / "package.yml").write_text(
        yaml.safe_dump({"components": ["fixture"]})
    )
    return host_mode.render_host("daniel-server", root)


def test_a_role_outside_the_allowlist_is_refused(tmp_path):
    with pytest.raises(RenderRefused, match="role 'k3s' is not one of"):
        render_with(tmp_path, {"role": "k3s", "files": []})


def test_a_role_template_with_no_role_is_refused(tmp_path):
    output = {
        "src": "role:host-upsmon.conf.j2",
        "path": CONF,
        "owner": "root",
        "group": "nut",
        "mode": "0640",
        "keys": ["nut_monitor_password"],
    }
    component = {"outputs": [output]}
    with pytest.raises(RenderRefused, match="names a role, and it lists none"):
        render_with(tmp_path, component)
