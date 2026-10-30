"""daniel-server's Claude subjects ship in its host package, and the role ends there (H7.39).

The component renders the claude_code role's templates and copies its scripts in place, so
these tests hold what the package adds: the agent user and its live uid, the packaged unit
names each unit points at, the units it leaves disabled, and the Ansible-era units it retires.

Run: uv run pytest hosts/lib/tests/test_claude_code_component.py
"""

import json
import shutil
import subprocess

import pytest
import yaml

from lib.estate import Inventory
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from secrets_mgmt import host_secrets
from setup_lib import setup_gates

ROLE = REPO / "ansible" / "roles" / "setup" / "claude_code"
UNITS = "/usr/lib/systemd/system"
# Every unit the role installed on daniel-server, read from `systemctl list-unit-files 'claude*'`
# there on 2026-10-29.
ANSIBLE_UNITS = {
    "claude-rc.service",
    "claude-rc-alert.service",
    "claude-rc-restart.service",
    "claude-rc-restart.timer",
    "claude-bg-resume.service",
    "claude-cgroup-metrics.service",
    "claude-cgroup-metrics.timer",
    "claude-clone-sync.service",
    "claude-clone-sync.timer",
    "claude-clone-sync-alert.service",
    "claude-dotfiles-sync.service",
    "claude-dotfiles-sync.timer",
    "claude-dotfiles-sync-alert.service",
    "claude-stop-alert@.service",
    "homelab-rootq.socket",
    "homelab-rootq@.service",
}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-server")


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


def text(package, path: str) -> str:
    return package.files[path][0].decode()


def test_the_agent_user_is_declared_with_its_live_uid(package, manifest):
    assert manifest["users"]["users"] == [
        {
            "name": "claude",
            "uid": 995,
            "home": "/var/lib/claude",
            "shell": "/bin/bash",
            "comment": "Claude agent",
        }
    ]
    assert text(package, "/usr/lib/sysusers.d/homelab.conf") == (
        'u claude 995 "Claude agent" /var/lib/claude /bin/bash\n'
    )


def test_the_role_ends_on_daniel_server_and_nowhere_else():
    hosts = ["daniel-box", "daniel-server", "daniel-pi"]
    assert setup_gates.role_end_hosts(ROLE, hosts) == frozenset({"daniel-server"})


def test_the_remote_control_host_ships_disabled_and_restarts_only_by_hand(
    package, manifest
):
    assert f"{UNITS}/homelab-claude-rc.service" in package.files
    assert "homelab-claude-rc.service" not in manifest["enable_links"]
    assert "homelab-claude-rc-restart.timer" not in manifest["enable_links"]
    assert manifest["units"]["homelab-claude-rc.service"] == {"policy": "manual"}
    assert {
        "homelab-claude-agent.service",
        "homelab-claude-cgroup-metrics.timer",
        "homelab-claude-clone-sync.timer",
        "homelab-claude-dotfiles-sync.timer",
        "homelab-rootq.socket",
    } <= set(manifest["enable_links"])


def test_each_packaged_unit_names_its_packaged_sibling(package):
    rc = text(package, f"{UNITS}/homelab-claude-rc.service")
    assert "\nOnFailure=homelab-claude-rc-alert.service\n" in rc
    restart = text(package, f"{UNITS}/homelab-claude-rc-restart.service")
    assert "try-restart homelab-claude-rc.service\n" in restart
    timer = text(package, f"{UNITS}/homelab-claude-cgroup-metrics.timer")
    assert "\nUnit=homelab-claude-cgroup-metrics.service\n" in timer
    for sync in ("clone-sync", "dotfiles-sync"):
        unit = text(package, f"{UNITS}/homelab-claude-{sync}.service")
        assert "\nOnFailure=homelab-alert@%n.service\n" in unit
        # Observability's host-checks rules read the textfile by its Ansible-era name.
        assert f"write_check_textfile claude-{sync} 1 " in unit


def test_every_ansible_era_unit_is_retired(manifest):
    retired = {entry["unit"] for entry in manifest["legacy"] if "unit" in entry}
    assert ANSIBLE_UNITS <= retired


def test_the_agents_github_token_is_a_host_secret_and_never_rendered(package):
    keys = host_secrets.tree_keys(REPO, "daniel-server")
    assert {"claude_code_agent_gh_token", "gitops_deploy_discord_webhook"} <= keys
    template = text(
        package, "/usr/lib/homelab/secret-templates/var/lib/claude/.config/gh/hosts.yml"
    )
    assert "${claude_code_agent_gh_token}" in template


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


def test_a_role_file_is_copied_from_the_roles_files(tmp_path):
    files = [{"src": "role-file:rootq.sh", "dst": "/usr/lib/homelab/x"}]
    package = render_with(tmp_path, {"role": "claude_code", "files": files})
    assert (
        package.files["/usr/lib/homelab/x"][0]
        == (ROLE / "files" / "rootq.sh").read_bytes()
    )


def test_a_role_file_that_names_a_template_is_refused(tmp_path):
    files = [{"src": "role-file:claude-rc.service.j2", "dst": "/usr/lib/homelab/x"}]
    with pytest.raises(RenderRefused, match="is a template; name it role:"):
        render_with(tmp_path, {"role": "claude_code", "files": files})


def test_a_file_whose_when_is_false_is_left_out(tmp_path):
    files = [
        {"src": "role-file:rootq.sh", "dst": "/usr/lib/homelab/on", "when": "x_on"},
        {
            "src": "role-file:rootq.sh",
            "dst": "/usr/lib/homelab/off",
            "when": "not x_on",
        },
    ]
    spec = {"role": "claude_code", "defaults": {"x_on": True}, "files": files}
    package = render_with(tmp_path, spec)
    assert "/usr/lib/homelab/on" in package.files
    assert "/usr/lib/homelab/off" not in package.files


def test_a_when_that_is_not_a_boolean_is_refused(tmp_path):
    files = [
        {"src": "role-file:rootq.sh", "dst": "/usr/lib/homelab/x", "when": "'false'"}
    ]
    with pytest.raises(RenderRefused, match="not a boolean"):
        render_with(tmp_path, {"role": "claude_code", "files": files})


BG_RESUME = (
    f"{UNITS}/homelab-claude-bg-resume.service",
    "/usr/local/lib/claude-bg-resume/bg_session_resume.py",
)


def render_daniel_server_with(tmp_path, **overrides):
    """daniel-server rendered with `overrides` laid over its host_vars."""
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO / "ansible" / "inventory" / "host_vars", host_vars)
    path = host_vars / "daniel-server.yml"
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), **overrides}))
    return host_mode.render_host(
        "daniel-server", inventory=Inventory(host_vars=host_vars)
    )


def test_the_background_resume_ships_while_its_toggle_is_on(package, manifest):
    assert all(path in package.files for path in BG_RESUME)
    assert manifest["units"]["homelab-claude-bg-resume.service"] == {"policy": "manual"}
    assert "homelab-claude-bg-resume.service" in manifest["enable_links"]


def test_the_background_resume_toggle_off_drops_its_unit_script_policy_and_link(
    tmp_path,
):
    off = render_daniel_server_with(tmp_path, claude_code_bg_resume_enabled=False)
    off_manifest = json.loads(off.files["/usr/lib/homelab/manifest.json"][0])
    assert not any(path in off.files for path in BG_RESUME)
    assert "homelab-claude-bg-resume.service" not in off_manifest["units"]
    assert "homelab-claude-bg-resume.service" not in off_manifest["enable_links"]


AUTHORIZED_KEYS = "/usr/lib/homelab/claude-agent/authorized_keys"
SETUP = "/usr/lib/homelab/claude-agent-setup"
REMOVAL = 'rm -f "$home/.ssh/authorized_keys"'
INSTALL = 'install -o root -g root -m 0644 "$templates/authorized_keys"'
OLD_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLDOLD peers"
)
NEW_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEW peers"
)


def test_the_peer_login_off_ships_no_key_and_the_setup_removes_the_old_one(tmp_path):
    package = render_daniel_server_with(
        tmp_path,
        claude_agent_ssh_login_enabled=False,
        claude_agent_peer_public_key=OLD_KEY,
    )
    assert AUTHORIZED_KEYS not in package.files
    setup = text(package, SETUP)
    assert REMOVAL in setup
    assert INSTALL not in setup
    # `usermod -G` sets the whole list, so leaving ssh-users out drops the membership.
    assert "ssh-users" not in setup


def test_a_rotated_peer_key_replaces_the_old_one_in_authorized_keys(tmp_path):
    package = render_daniel_server_with(
        tmp_path,
        claude_agent_ssh_login_enabled=True,
        claude_agent_peer_public_key=NEW_KEY,
    )
    assert package.files[AUTHORIZED_KEYS][0].decode() == f"restrict {NEW_KEY}\n"
    setup = text(package, SETUP)
    assert INSTALL in setup
    assert REMOVAL not in setup
    assert "ssh-users" in setup


def test_a_blank_peer_key_ships_no_file_and_leaves_the_one_ansible_wrote(tmp_path):
    package = render_daniel_server_with(tmp_path, claude_agent_ssh_login_enabled=True)
    assert AUTHORIZED_KEYS not in package.files
    assert REMOVAL not in text(package, SETUP)


def test_the_agent_toggle_and_the_users_entry_agree():
    # The users arm takes no `when:`. Switching the agent off on daniel-server means deleting
    # the component's `users:` entry, which the arm then expires.
    host_vars = REPO / "ansible" / "inventory" / "host_vars" / "daniel-server.yml"
    enabled = yaml.safe_load(host_vars.read_text())["claude_code_agent_user_enabled"]
    component = REPO / "hosts" / "components" / "claude-code" / "component.yml"
    declared = {u["name"] for u in yaml.safe_load(component.read_text())["users"]}
    assert enabled == ("claude" in declared)


VISUDO = shutil.which("visudo") or "/usr/sbin/visudo"


def visudo_check(tmp_path, grant: bytes) -> subprocess.CompletedProcess:
    path = tmp_path / "grant"
    path.write_bytes(grant)
    path.chmod(0o440)
    return subprocess.run([VISUDO, "-cf", str(path)], capture_output=True, text=True)


def test_the_packaged_sudoers_grant_parses(package, tmp_path):
    # Ansible validated the grant with `visudo -cf`; dpkg validates nothing, and a grant sudo
    # cannot parse breaks sudo on the host.
    grant = package.files["/etc/sudoers.d/homelab-rootq"][0]
    result = visudo_check(tmp_path, grant)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_malformed_sudoers_grant_is_flagged(tmp_path):
    result = visudo_check(tmp_path, b"ubu ntu ALL=(root NOPASSWD: /x\n")
    assert result.returncode != 0
