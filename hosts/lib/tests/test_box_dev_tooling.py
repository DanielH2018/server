"""daniel-box's dev tooling, chezmoi_setup and github_cli move out of Ansible (H7.62).

The worktree holder scan's half of the row is `ansible/tests/setup/test_worktree_holders_install.py`.

Run: uv run pytest hosts/lib/tests/test_box_dev_tooling.py
"""

import json
import subprocess

import pytest

from deploy_tools.setup_lib.setup_census import items_left
from lib import yaml_fast
from lib.repo_paths import ANSIBLE, REPO
from renderer.renderer_lib import host as host_mode

HOST = "daniel-box"
ROLES = ANSIBLE / "roles" / "setup"
ROLE = ROLES / "initial_setup"
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
    "ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2",
    "hosts/components/k3s-host-checks/templates/longhorn-backup-health.sh.j2",
    "ansible/roles/setup/common/tasks/release_bin.yml",
)


@pytest.fixture(scope="module")
def box() -> host_mode.Package:
    return host_mode.render_host(HOST)


def _legacy_paths(package: host_mode.Package) -> set[str]:
    manifest = json.loads(package.files["/usr/lib/homelab/manifest.json"][0])
    return {entry.get("path") for entry in manifest["legacy"]}


def test_vale_reaches_daniel_box_and_retires_the_old_copy(box):
    body, mode, *_ = box.files["/usr/bin/vale"]
    assert mode == 0o755 and 'exec /usr/lib/homelab/bin/vale "$@"' in body.decode()
    assert "/usr/local/bin/vale" in _legacy_paths(box)


def test_daniel_box_retires_the_old_uv_copy(box):
    assert OLD_UV in _legacy_paths(box)


def test_every_setup_role_reader_names_the_packaged_uv():
    readers = subprocess.run(
        ["git", "grep", "-l", OLD_UV, "--", "ansible/roles", "scripts/backup"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    ).stdout.split()
    assert readers == []
    for reader in MOVED_UV_READERS:
        assert "/usr/bin/uv" in (REPO / reader).read_text()


def test_the_package_depends_on_what_the_roles_installed(box):
    assert DISTRO_PACKAGES <= set(box.depends)
    assert "gh" in box.depends
    assert "gh" in host_mode.render_host("daniel-server").depends


@pytest.mark.parametrize("role", ["chezmoi_setup", "github_cli"])
def test_the_role_applies_as_a_no_op(role):
    tasks = yaml_fast.safe_load((ROLES / role / "tasks" / "main.yml").read_text())
    assert tasks == [
        {
            "name": "End the role, whose work the host packages and the dotfiles repo own",
            "ansible.builtin.meta": "end_role",
        }
    ]


def test_the_census_holds_no_item_of_the_moved_subjects():
    items = items_left((HOST,))[HOST]
    left = [
        str(item)
        for item in items
        if item.role in {"chezmoi_setup", "github_cli"}
        or any(
            word in str(item)
            for word in (
                "Install Git hooks",
                "Install prek",
                "Vale",
                "/usr/local/libexec",
                "worktree-holders.json",
            )
        )
    ]
    assert items, "the census found nothing on daniel-box"
    assert left == []


def test_the_map_drops_the_subjects_that_have_no_tasks_left():
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    map_ = defaults.get("initial_setup_packaged_hosts", {})
    assert not {"dev_tooling", "vale", "worktree_holders"} & set(map_)
    assert "initial_setup_uv_version" not in defaults


def test_the_agent_takes_chezmoi_from_homelab_tools(box):
    # A rebuilt host has no ~/.local/bin/chezmoi: the dotfiles repo does not install that path.
    setup = next(
        body.decode()
        for path, (body, *_) in box.files.items()
        if path.endswith("/claude-agent-setup")
    )
    assert '/usr/lib/homelab/bin/chezmoi "$home/.local/bin/chezmoi"' in setup
    assert "/home/$operator/.local/bin/chezmoi" not in setup
