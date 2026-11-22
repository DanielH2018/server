"""daniel-box ships the same three `initial_setup` subjects as the Pi.

The components are host-neutral and `test_pi_sshd_apt_and_tuning.py` holds their content. These
tests hold what is specific to this host: the package names the components, and its sshd Match
block names its own sys user.

Run: uv run pytest hosts/lib/tests/test_box_sshd_apt_and_tuning.py
"""

import pytest

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from renderer.renderer_lib import host as host_mode

SSHD_D = "/etc/ssh/sshd_config.d"
SYSCTL = "/etc/sysctl.d/90-homelab-host-tuning.conf"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def test_the_package_ships_the_three_components_and_activates_ssh_and_journald(package):
    assert package.activations[f"{SSHD_D}/10-homelab.conf"] == "reload:ssh.service"
    assert package.activations[SYSCTL] == "sysctl"
    assert (
        package.activations["/etc/systemd/journald.conf.d/50-homelab.conf"]
        == "try-restart:systemd-journald.service"
    )
    assert "/etc/apt/apt.conf.d/52unattended-upgrades-local" in package.files


def test_the_apt_origin_patterns_include_the_servers_github_cli_origin(package):
    patterns = render_context(None, "daniel-box", inventory=REPO_INVENTORY)[
        "unattended_upgrades_origins_patterns"
    ]
    local = package.files["/etc/apt/apt.conf.d/52unattended-upgrades-local"][0].decode()
    assert "origin=gh,codename=stable" in patterns
    for pattern in patterns:
        assert f'Origins-Pattern:: "{pattern}";' in local


def test_the_match_block_names_the_hosts_sys_user(package):
    user = render_context(None, "daniel-box", inventory=REPO_INVENTORY)["sys_user"]
    match = package.files[f"{SSHD_D}/90-homelab-match.conf"][0].decode()
    assert f"Match User {user}\n" in match
