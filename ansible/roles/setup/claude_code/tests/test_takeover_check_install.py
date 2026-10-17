"""What tasks/takeover_check.yml installs and removes for the takeover check wrapper (#5494).

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_takeover_check_install.py
"""

import shutil

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast
from lib.proc_testing import run as run_proc
from lib.repo_paths import HOST_VARS

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
WRAPPER = "/usr/local/sbin/homelab-takeover-check"


def test_the_sudoers_template_grants_one_command_per_user() -> None:
    rendered = render_setup_text(
        "claude_code",
        "claude-takeover-check.sudoers.j2",
        {"claude_code_takeover_check_users": ["ubuntu", "claude"]},
    )
    lines = [x for x in rendered.splitlines() if x and not x.startswith("#")]
    assert lines == [
        f"ubuntu ALL=(root) NOPASSWD: {WRAPPER}",
        f"claude ALL=(root) NOPASSWD: {WRAPPER}",
    ]
    assert rendered.count(WRAPPER) == 2


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo is not installed")
def test_the_rendered_sudoers_file_passes_visudo(tmp_path) -> None:
    rendered = render_setup_text(
        "claude_code",
        "claude-takeover-check.sudoers.j2",
        {"claude_code_takeover_check_users": ["ubuntu"]},
    )
    path = tmp_path / "claude-takeover-check"
    path.write_text(rendered)
    path.chmod(0o440)
    result = run_proc(["visudo", "-cf", str(path)])
    assert result.returncode == 0, result.stdout + result.stderr


def tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "takeover_check.yml").read_text())


def test_disabling_removes_the_grant_the_wrapper_the_flux_copy_and_the_state_dir_in_order() -> (
    None
):
    (install, remove) = tasks()
    assert install["when"] == "claude_code_takeover_check_enabled"
    assert remove["when"] == "not claude_code_takeover_check_enabled"
    removed = [
        (t["ansible.builtin.file"]["path"], t["ansible.builtin.file"]["state"])
        for t in remove["block"]
    ]
    assert removed == [
        ("/etc/sudoers.d/claude-takeover-check", "absent"),
        ("{{ claude_code_takeover_check_wrapper }}", "absent"),
        ("{{ claude_code_takeover_check_lib_dir }}", "absent"),
        ("{{ claude_code_takeover_check_state_dir }}", "absent"),
    ]
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults["claude_code_takeover_check_wrapper"] == WRAPPER
    assert (
        defaults["claude_code_takeover_check_lib_dir"]
        == "/usr/local/lib/homelab-takeover-check"
    )
    assert (
        defaults["claude_code_takeover_check_state_dir"]
        == "/var/lib/homelab-takeover-check"
    )
    assert defaults["claude_code_takeover_check_enabled"] is False


def test_enabling_installs_the_grant_last_and_validates_it() -> None:
    (install, _) = tasks()
    grant = install["block"][-1]
    assert (
        grant["ansible.builtin.template"]["dest"]
        == "/etc/sudoers.d/claude-takeover-check"
    )
    assert grant["ansible.builtin.template"]["mode"] == "0440"
    assert grant["ansible.builtin.template"]["validate"] == "visudo -cf %s"
    assert all(t["become"] is True for t in install["block"])


def test_the_flux_copy_is_pinned_to_the_flux_the_cluster_runs() -> None:
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    flux = yaml_fast.safe_load(
        (ANSIBLE / "roles" / "k8s" / "flux" / "defaults" / "main.yml").read_text()
    )
    assert (
        f"/download/{flux['flux_version']}/"
        in defaults["claude_code_takeover_check_flux_url"]
    )
    assert len(defaults["claude_code_takeover_check_flux_sha256"]) == 64
    (install, _) = tasks()
    download = next(t for t in install["block"] if "ansible.builtin.get_url" in t)
    assert download["ansible.builtin.get_url"]["checksum"].startswith("sha256:")


def test_only_daniel_box_enables_the_check() -> None:
    hosts = HOST_VARS
    enabled = {
        p.stem
        for p in hosts.glob("*.yml")
        if (yaml_fast.safe_load(p.read_text()) or {}).get(
            "claude_code_takeover_check_enabled"
        )
    }
    assert enabled == {"daniel-box"}
