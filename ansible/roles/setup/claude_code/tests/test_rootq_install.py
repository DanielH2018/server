"""What tasks/rootq.yml installs and removes for the read-only root inspector (#6323).

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_rootq_install.py
"""

import hashlib
import shutil

import homelab_rootq_socket
import pytest
import rootq_socket
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast
from lib.git_testing import init_repo, scrubbed_env
from lib.proc_testing import run as run_proc
from lib.proc_testing import write_exec
from lib.repo_paths import HOST_VARS

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
SCRIPT = "/usr/local/sbin/homelab-rootq"
SUDOERS = "/etc/sudoers.d/claude-rootq"


def render_sudoers(users: list[str]) -> str:
    return render_setup_text(
        "claude_code", "claude-rootq.sudoers.j2", {"claude_code_rootq_users": users}
    )


def test_the_sudoers_template_grants_one_command_per_user() -> None:
    rendered = render_sudoers(["ubuntu", "claude"])
    lines = [x for x in rendered.splitlines() if x and not x.startswith("#")]
    assert lines == [
        f"ubuntu ALL=(root) NOPASSWD: {SCRIPT}",
        f"claude ALL=(root) NOPASSWD: {SCRIPT}",
    ]


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo is not installed")
def test_the_rendered_sudoers_file_passes_visudo(tmp_path) -> None:
    path = tmp_path / "claude-rootq"
    path.write_text(render_sudoers(["ubuntu"]))
    path.chmod(0o440)
    result = run_proc(["visudo", "-cf", str(path)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_shim_runs_only_the_granted_script_through_sudo() -> None:
    lines = (ROLE / "files" / "rootq.sh").read_text().splitlines()
    assert [x.strip() for x in lines if "$sudo" in x and not x.startswith("#")] == [
        'exec "$sudo" -n "$script" matches "$2" "$hash" < /dev/null',
        'exec "$sudo" -n "$script" "$@" < /dev/null',
    ]
    assert "sudo=${HOMELAB_ROOTQ_SUDO:-/usr/bin/sudo}" in lines
    assert f"script={SCRIPT}" in lines
    assert lines[-1].startswith("exec ")


@pytest.fixture
def shim(tmp_path):
    """The shim run inside a scratch checkout, with sudo replaced by a recorder of its argv."""
    repo = init_repo(tmp_path / "repo")
    (repo / "conf").mkdir()
    (repo / "conf" / "a.yaml").write_text("network:\n  version: 2\n")
    (repo / "out.yaml").symlink_to("/etc/hostname")
    log = tmp_path / "sudo.argv"
    sudo = write_exec(tmp_path / "sudo", f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n')

    def run(*args: str):
        result = run_proc(
            ["sh", str(ROLE / "files" / "rootq.sh"), *args],
            cwd=repo,
            env=scrubbed_env(HOMELAB_ROOTQ_SUDO=str(sudo)),
        )
        return result, log.read_text().splitlines() if log.exists() else None

    return run


def test_matches_hands_root_only_the_hash_of_the_repo_file(shim) -> None:
    result, argv = shim("matches", "/etc/netplan/a.yaml", "conf/a.yaml")
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(b"network:\n  version: 2\n").hexdigest()
    assert argv == ["-n", SCRIPT, "matches", "/etc/netplan/a.yaml", digest]


def test_matches_hands_root_a_hash_given_in_place_of_the_repo_file(shim) -> None:
    """`probe.py applied` asks this way on a host with no checkout (#6357)."""
    digest = "ab" * 32
    result, argv = shim("matches", "/etc/netplan/a.yaml", digest)
    assert result.returncode == 0, result.stderr
    assert argv == ["-n", SCRIPT, "matches", "/etc/netplan/a.yaml", digest]


@pytest.mark.parametrize("almost", ["ab" * 31, "AB" * 32, "ab" * 32 + "0"])
def test_matches_reads_a_near_hash_as_a_repo_file(shim, almost) -> None:
    result, argv = shim("matches", "/etc/netplan/a.yaml", almost)
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    assert argv is None


def test_every_other_verb_passes_its_arguments_through_unchanged(shim) -> None:
    _, argv = shim("cat", "/etc/netplan/a.yaml")
    assert argv == ["-n", SCRIPT, "cat", "/etc/netplan/a.yaml"]


@pytest.mark.parametrize(
    "repo_file, reason",
    [
        ("/etc/hostname", "relative"),
        ("../repo/conf/a.yaml", "relative"),
        ("conf/../conf/a.yaml", "relative"),
        ("..", "relative"),
        ("conf/missing.yaml", "does not exist"),
        ("conf", "not a regular file"),
        ("out.yaml", "outside the checkout"),
    ],
)
def test_matches_refuses_a_repo_file_it_cannot_vouch_for(
    shim, repo_file, reason
) -> None:
    result, argv = shim("matches", "/etc/netplan/a.yaml", repo_file)
    assert result.returncode == 2
    assert reason in result.stderr
    assert argv is None


def tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "rootq.yml").read_text())


def test_enabling_installs_the_grant_last_and_validates_it() -> None:
    install = tasks()[0]
    assert install["when"] == "claude_code_rootq_enabled"
    copies = [t["ansible.builtin.copy"] for t in install["block"][:-1]]
    assert [(c["dest"], c["owner"], c["mode"]) for c in copies] == [
        ("{{ claude_code_rootq_script }}", "root", "0755"),
        ("{{ claude_code_rootq_shim }}", "root", "0755"),
    ]
    grant = install["block"][-1]["ansible.builtin.template"]
    assert (grant["dest"], grant["mode"], grant["validate"]) == (
        SUDOERS,
        "0440",
        "visudo -cf %s",
    )
    assert all(t["become"] is True for t in install["block"])


def test_disabling_removes_the_grant_the_shim_and_the_script_in_order() -> None:
    remove = tasks()[-1]
    assert remove["when"] == "not claude_code_rootq_enabled"
    removed = [
        (t["ansible.builtin.file"]["path"], t["ansible.builtin.file"]["state"])
        for t in remove["block"]
    ]
    assert removed == [
        (SUDOERS, "absent"),
        ("{{ claude_code_rootq_shim }}", "absent"),
        ("{{ claude_code_rootq_script }}", "absent"),
    ]
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults["claude_code_rootq_enabled"] is False
    assert defaults["claude_code_rootq_script"] == SCRIPT
    assert defaults["claude_code_rootq_shim"] == "/usr/local/bin/rootq"
    # The operator's account only: the agent user's sudo grant became a socket in #4297.
    assert defaults["claude_code_rootq_users"] == ["{{ sys_user }}"]


def test_daniel_box_and_daniel_server_enable_the_inspector() -> None:
    enabled = {
        p.stem
        for p in HOST_VARS.glob("*.yml")
        if (yaml_fast.safe_load(p.read_text()) or {}).get("claude_code_rootq_enabled")
    }
    assert enabled == {"daniel-box", "daniel-server"}


# ── The agent user's socket (#6350) ───────────────────────────────────────────────────────


def directives(template: str) -> dict[str, str]:
    """`Key=Value` for every directive line of a rendered unit. A repeated key keeps its last."""
    found = {}
    for line in render_setup_text("claude_code", template).splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip()
    return found


def defaults() -> dict:
    return yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())


def test_the_socket_admits_only_its_group_and_runs_the_installed_helper() -> None:
    socket = directives("homelab-rootq.socket.j2")
    service = directives("homelab-rootq@.service.j2")
    d = defaults()
    assert socket["ListenStream"] == d["claude_code_rootq_socket_path"]
    assert socket["ListenStream"] == rootq_socket.SOCKET
    assert (socket["Accept"], socket["SocketUser"], socket["SocketMode"]) == (
        "yes",
        "root",
        "0660",
    )
    assert socket["SocketGroup"] == d["claude_code_rootq_socket_group"]
    assert service["ExecStart"] == d["claude_code_rootq_socket_helper"]
    assert service["StandardInput"] == "socket"
    # Only the helper's own sendall may reach the socket; a traceback goes to the journal.
    assert (service["StandardOutput"], service["StandardError"]) == (
        "journal",
        "journal",
    )
    assert service["NoNewPrivileges"] == "yes"
    assert int(service["RuntimeMaxSec"]) > homelab_rootq_socket.RUN_TIMEOUT_S


def test_the_service_keeps_the_hosts_network_namespace_for_nft_and_ufw() -> None:
    """A private namespace would make `nft list ruleset` print an empty ruleset, not refuse."""
    service = directives("homelab-rootq@.service.j2")
    assert not {
        "PrivateNetwork",
        "RestrictAddressFamilies",
        "CapabilityBoundingSet",
    } & set(service)


def test_the_helper_runs_the_script_the_sudo_grant_names() -> None:
    assert homelab_rootq_socket.SCRIPT == SCRIPT
    assert defaults()["claude_code_rootq_script"] == SCRIPT


def socket_blocks() -> tuple[dict, dict]:
    (install, install_socket, remove_socket, remove) = tasks()
    assert install["when"] == "claude_code_rootq_enabled"
    assert remove["when"] == "not claude_code_rootq_enabled"
    return install_socket, remove_socket


def test_the_socket_exists_only_while_the_script_it_runs_does() -> None:
    install, remove = socket_blocks()
    assert install["when"] == "claude_code_rootq_socket_armed"
    assert remove["when"] == "not claude_code_rootq_socket_armed"
    d = defaults()
    assert d["claude_code_rootq_socket_enabled"] is False
    assert d["claude_code_rootq_socket_armed"] == (
        "{{ claude_code_rootq_enabled and claude_code_rootq_socket_enabled }}"
    )


def test_enabling_installs_the_helper_the_client_and_the_units_before_listening() -> (
    None
):
    install, _ = socket_blocks()
    names = [t["name"] for t in install["block"]]
    assert names[0].startswith("Create the group")
    assert names[-1].startswith("Listen on")
    copies = [
        t["ansible.builtin.copy"]
        for t in install["block"]
        if "ansible.builtin.copy" in t
    ]
    assert {c["dest"]: (c["owner"], c["mode"]) for c in copies} == {
        "{{ claude_code_rootq_socket_helper }}": ("root", "0700"),
        "{{ claude_code_rootq_socket_client }}": ("root", "0755"),
    }
    listen = install["block"][-1]["ansible.builtin.systemd_service"]
    assert (listen["name"], listen["enabled"]) == ("homelab-rootq.socket", True)
    assert all(t["become"] is True for t in install["block"])


def test_disabling_stops_the_socket_before_its_units_go_and_removes_the_group_last() -> (
    None
):
    _, remove = socket_blocks()
    steps = remove["block"]
    stop = next(
        i for i, t in enumerate(steps) if "ansible.builtin.systemd_service" in t
    )
    unit_files = next(i for i, t in enumerate(steps) if "ansible.builtin.file" in t)
    assert steps[stop]["ansible.builtin.systemd_service"]["state"] == "stopped"
    assert steps[stop]["when"] == "claude_code_rootq_socket_unit.stat.exists"
    assert stop < unit_files
    assert steps[unit_files]["ansible.builtin.file"]["state"] == "absent"
    group = steps[-1]["ansible.builtin.group"]
    assert (group["name"], group["state"]) == (
        "{{ claude_code_rootq_socket_group }}",
        "absent",
    )


def test_the_agent_brief_names_the_socket_client_only_while_the_socket_is_armed() -> (
    None
):
    def brief(armed: bool) -> str:
        return render_setup_text(
            "claude_code",
            "agent-user-claude-md.j2",
            {"claude_code_rootq_socket_armed": armed},
        )

    assert "`rootq-socket`" in brief(True)
    assert "rootq-socket" not in brief(False)
    assert "ask the operator to run `rootq`" in brief(False)


def test_daniel_box_and_daniel_server_arm_the_socket() -> None:
    armed = {
        p.stem
        for p in HOST_VARS.glob("*.yml")
        if (yaml_fast.safe_load(p.read_text()) or {}).get(
            "claude_code_rootq_socket_enabled"
        )
    }
    assert armed == {"daniel-box", "daniel-server"}
