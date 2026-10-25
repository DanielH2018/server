"""What tasks/rootq.yml installs and removes for the read-only root inspector (#6323).

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_rootq_install.py
"""

import shutil

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast
from lib.proc_testing import run as run_proc
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
    assert [x for x in lines if "sudo " in x and not x.startswith("#")] == [
        f'exec /usr/bin/sudo -n {SCRIPT} "$@" < /dev/null'
    ]
    assert lines[-1].startswith("exec ")


def tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "rootq.yml").read_text())


def test_enabling_installs_the_grant_last_and_validates_it() -> None:
    (install, _) = tasks()
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
    (_, remove) = tasks()
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
