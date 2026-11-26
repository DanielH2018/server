"""daniel-box's dev tooling, chezmoi_setup and github_cli move out of Ansible.

The worktree holder scan's half of the row is `hosts/tests/repo_crons/test_worktree_holders_install.py`.

Run: uv run pytest hosts/lib/tests/test_box_dev_tooling.py
"""

import subprocess

import pytest

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

HOST = "daniel-box"
# What the dotfiles' install-cli-tools script cannot install itself.
DISTRO_PACKAGES = {
    "bat",
    "fd-find",
    "gron",
    "shellcheck",
    "lua5.4",
    "btop",
    "chafa",
    "zsh",
    "zsh-autosuggestions",
    "zsh-syntax-highlighting",
}
OLD_UV = "/usr/local/bin/uv"
# Readers the census must have moved, so a narrowed grep cannot pass over nothing.
MOVED_UV_READERS = (
    "hosts/components/k3s-host-checks/templates/longhorn-backup-health.sh.j2",
)


@pytest.fixture(scope="module")
def box() -> host_mode.Package:
    return host_mode.render_host(HOST)


def test_vale_reaches_daniel_box(box):
    body, mode, *_ = box.files["/usr/bin/vale"]
    assert mode == 0o755 and 'exec /usr/lib/homelab/bin/vale "$@"' in body.decode()


def test_every_setup_role_reader_names_the_packaged_uv():
    readers = subprocess.run(
        ["git", "grep", "-l", OLD_UV, "--", "k8s/roles", "scripts/backup"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        timeout=60,
    ).stdout.split()
    assert readers == []
    for reader in MOVED_UV_READERS:
        assert "/usr/bin/uv" in (REPO / reader).read_text()


def test_the_package_depends_on_what_the_roles_installed(box):
    assert DISTRO_PACKAGES <= set(box.depends)
    assert "gh" in box.depends
    assert "gh" in host_mode.render_host("daniel-server").depends


def test_the_agent_takes_chezmoi_from_homelab_tools(box):
    # A rebuilt host has no ~/.local/bin/chezmoi: the dotfiles repo does not install that path.
    setup = next(
        body.decode()
        for path, (body, *_) in box.files.items()
        if path.endswith("/claude-agent-setup")
    )
    assert '/usr/lib/homelab/bin/chezmoi "$home/.local/bin/chezmoi"' in setup
    assert "/home/$operator/.local/bin/chezmoi" not in setup
