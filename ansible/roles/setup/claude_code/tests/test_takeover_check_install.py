"""What the takeover check wrapper ships and pins (#5494).

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
TOOLS = ANSIBLE.parent / "hosts" / "packages" / "homelab-tools" / "package.yml"


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


def test_the_defaults_leave_the_check_off_and_name_its_wrapper_and_state() -> None:
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults["claude_code_takeover_check_wrapper"] == WRAPPER
    assert (
        defaults["claude_code_takeover_check_state_dir"]
        == "/var/lib/homelab-takeover-check"
    )
    assert defaults["claude_code_takeover_check_enabled"] is False
    # homelab-tools pins the flux CLI, so no second pin or copy directory may come back here.
    assert [
        k
        for k in defaults
        if k.startswith("claude_code_takeover_check_flux")
        or k == "claude_code_takeover_check_lib_dir"
    ] == []


def test_homelab_tools_pins_the_flux_the_cluster_runs() -> None:
    flux = yaml_fast.safe_load(
        (ANSIBLE / "roles" / "k8s" / "flux" / "defaults" / "main.yml").read_text()
    )["flux_version"]
    tools = yaml_fast.safe_load(TOOLS.read_text())["tools"]
    assert {arch: pin["version"] for arch, pin in tools["flux"].items()} == {
        "amd64": flux,
        "arm64": flux,
    }


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
