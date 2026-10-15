"""`nfpm.yaml` rules a host package spec renders to: enable links as symlinks, and the version kept as given.

systemd adds a `Wants=` for each unit *symlinked* from a `<target>.wants/` directory, so a spec
declares an enable link under `enable:` and the renderer ships it as an nfpm `type: symlink`
entry (#4942, #5005). nfpm's default `version_schema: semver` rewrites
`0.<count>+g<sha>` to `0.<count>.0+g<sha>`, so the spec opts out of it (#4976).

Run: uv run pytest scripts/renderer/tests/test_renderer_host_package_rules.py
"""

import json
import os

import pytest

from lib import yaml_fast
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_manifest import MANIFEST

UNITS = "/usr/lib/systemd/system"


def _host(hosts, package: dict, where: str = "daniel-pi"):
    """A tree whose `where` spec is `package`, with its `u` source written beside it."""
    root = hosts(package, where=where)
    (root / where / "u").write_text("[Unit]\n")
    return root


def _contents(package) -> dict[str, dict]:
    spec = yaml_fast.safe_load(package.nfpm_spec())
    return {entry["dst"]: entry for entry in spec["contents"]}


@pytest.mark.parametrize(
    ("unit", "target"),
    [("homelab-u.timer", "timers.target"), ("homelab-x.service", "multi-user.target")],
)
def test_a_declared_enable_link_renders_an_nfpm_symlink_entry(hosts, unit, target):
    root = _host(
        hosts,
        {"files": [{"src": "u", "dst": f"{UNITS}/{unit}"}], "enable": {unit: target}},
    )

    entry = _contents(host_mode.render_host("daniel-pi", root)).get(
        f"{UNITS}/{target}.wants/{unit}"
    )

    assert entry is not None
    assert (entry.get("type"), entry.get("src")) == ("symlink", f"../{unit}")


def test_a_declared_enable_link_is_written_as_a_relative_symlink(hosts, tmp_path):
    root = _host(
        hosts,
        {
            "files": [{"src": "u", "dst": f"{UNITS}/homelab-u.timer"}],
            "enable": {"homelab-u.timer": "timers.target"},
        },
    )
    out = tmp_path / "out"

    host_mode.write_packages([host_mode.render_host("daniel-pi", root)], out)

    link = (
        out
        / "hosts/daniel-pi/root/usr/lib/systemd/system/timers.target.wants/homelab-u.timer"
    )
    assert link.is_symlink()
    assert os.readlink(link) == "../homelab-u.timer"


def test_a_plain_file_entry_under_a_wants_directory_is_flagged(hosts):
    # The package installs the unit, so the manifest's own check finds nothing to refuse.
    root = _host(
        hosts,
        {
            "files": [
                {"src": "u", "dst": f"{UNITS}/homelab-u.timer"},
                {"src": "u", "dst": f"{UNITS}/timers.target.wants/homelab-u.timer"},
            ]
        },
    )
    with pytest.raises(RenderRefused, match="wants"):
        host_mode.render_host("daniel-pi", root)


def test_a_host_s_declared_link_to_the_updater_s_timer_joins_its_manifest(hosts):
    # H3.9b's shape: the updater ships the timer, and the host package arms it.
    root = _host(
        hosts,
        {"files": [{"src": "u", "dst": f"{UNITS}/homelab-updater.timer"}]},
        where="packages/homelab-updater",
    )
    hosts({"enable": {"homelab-updater.timer": "timers.target"}})

    package = host_mode.render_host("daniel-pi", root)

    assert json.loads(package.files[MANIFEST][0])["enable_links"] == [
        "homelab-updater.timer"
    ]
    entry = _contents(package).get(f"{UNITS}/timers.target.wants/homelab-updater.timer")
    assert entry is not None
    assert (entry.get("type"), entry.get("src")) == (
        "symlink",
        "../homelab-updater.timer",
    )


@pytest.mark.parametrize("where", ["daniel-pi", "packages/homelab-updater"])
def test_the_nfpm_spec_keeps_the_version_string_as_given(hosts, where):
    root = _host(hosts, {"description": "x"}, where=where)
    render = host_mode.render_host if where == "daniel-pi" else host_mode.render_package

    spec = yaml_fast.safe_load(render(where.rsplit("/", 1)[-1], root).nfpm_spec())

    assert spec["version"] == "${HOMELAB_VERSION}"
    # `semver`, nfpm's default, builds `0.5513+gc9db6da8cdee` as `0.5513.0+gc9db6da8cdee`.
    assert spec.get("version_schema") == "none"


def test_a_shared_package_s_enable_link_is_flagged(hosts):
    # No manifest lists it, so reconcile would never start the unit it arms.
    root = hosts(
        {"enable": {"homelab-updater.timer": "timers.target"}},
        where="packages/homelab-updater",
    )
    with pytest.raises(RenderRefused, match="a shared package lists no enable link"):
        host_mode.render_package("homelab-updater", root)


def test_two_specs_declaring_one_enable_link_are_flagged(hosts):
    link = {"enable": {"homelab-u.timer": "timers.target"}}
    root = hosts({"components": ["beat"], **link}, {"beat": link})
    with pytest.raises(RenderRefused, match="two specs install"):
        host_mode.render_host("daniel-pi", root)
