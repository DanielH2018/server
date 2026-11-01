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
from renderer.renderer_lib import host as host_mode

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


def test_the_defaults_leave_the_check_off_and_name_its_wrapper_and_paths() -> None:
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


def test_the_flux_copy_is_pinned_to_the_flux_the_cluster_runs() -> None:
    defaults = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    flux = yaml_fast.safe_load(
        (ANSIBLE / "roles" / "k8s" / "flux" / "defaults" / "main.yml").read_text()
    )
    assert defaults["claude_code_takeover_check_flux_version"] == flux["flux_version"]
    url = defaults["claude_code_takeover_check_flux_url"]
    assert "/download/{{ claude_code_takeover_check_flux_version }}/" in url
    assert len(defaults["claude_code_takeover_check_flux_sha256"]) == 64
    script = (
        host_mode.render_host("daniel-box")
        .files["/usr/lib/homelab/takeover-check-flux"][0]
        .decode()
    )
    assert f'sha256="{defaults["claude_code_takeover_check_flux_sha256"]}"' in script


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
