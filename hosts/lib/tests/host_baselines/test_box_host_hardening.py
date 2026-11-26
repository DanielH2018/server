"""daniel-box's host baseline moves into its package, and the role's tasks for it go.

`test_pi_host_hardening.py` holds the `host-hardening` component's content against the bytes
the role wrote. These tests hold what is specific to the last host: daniel-box ships the same
bytes and takes no i915 option.

Run: uv run pytest hosts/lib/tests/host_baselines/test_box_host_hardening.py
"""

import pytest
from lib import yaml_fast
from lib.repo_paths import REPO
from test_pi_host_hardening import (
    BLACKLIST,
    BLACKLIST_WRITTEN,
    SUDOERS,
    SUDOERS_DIRECTIVE,
    WRITTEN,
    sudoers_directives,
)

from renderer.renderer_lib import host as host_mode

FILTER = "/etc/rsyslog.d/49-homelab-info-filter.conf"
I915 = "/etc/modprobe.d/i915.conf"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


@pytest.mark.parametrize("dst", sorted(WRITTEN))
def test_daniel_box_ships_the_bytes_the_role_wrote(package, dst):
    assert package.files[dst][0].decode() == WRITTEN[dst]


def test_daniel_box_ships_the_pi_sudoers_directive(package):
    assert sudoers_directives(package.files[SUDOERS][0]) == [SUDOERS_DIRECTIVE]


def test_daniel_box_ships_the_filter_and_adopts_the_blacklist(package):
    assert FILTER in package.files
    edits = [edit["line"] + "\n" for edit in package.lines if edit["path"] == BLACKLIST]
    assert "".join(edits) == BLACKLIST_WRITTEN


def test_the_has_igpu_gate_keeps_the_i915_option_off_daniel_box(package):
    spec = yaml_fast.safe_load(
        (REPO / "hosts" / "daniel-box" / "package.yml").read_text()
    )
    assert "host-hardening" in spec["components"]
    component = yaml_fast.safe_load(
        (REPO / "hosts" / "components" / "host-hardening" / "component.yml").read_text()
    )
    entry = next(f for f in component["files"] if f["dst"] == I915)
    assert entry["when"] == "has_igpu"
    assert "/etc/sudoers.d/10-timestamp" in package.files
    assert I915 not in package.files
