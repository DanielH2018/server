"""The packaged branch of the UPS watchdog, rendered the way a host package renders it (H4.3b3).

Since H4.3c, daniel-server's `nut-secondary` component sets `nut_host_watchdog_packaged` and
renders the role's template in host mode, where no secret exists. These tests render the template
that way for daniel-server directly, so the branch is known to render, to read no secret and to
be valid shell, whatever the component's file entry does with it.

Run: uv run pytest hosts/lib/tests/test_nut_secondary_packaged_watchdog.py
"""

import subprocess

import pytest

from lib import yaml_fast
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError, renderer_env

ROLE = ANSIBLE / "roles" / "setup" / "nut_host"
WATCHDOG = "ups-secondary-health.sh.j2"
HOST = "daniel-server"
# A SOPS key the packaged render must not reach.
SECRET = "ups_secondary_push_token"


def defaults() -> dict:
    """The role's defaults with the flag set as the component's `defaults:` map sets it."""
    role = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    return {**role, "nut_host_watchdog_packaged": True}


def render(directory, name: str = WATCHDOG) -> str:
    """`name` rendered in host mode for daniel-server, with every key of secrets.yml withheld."""
    ctx, reasons = host_mode.host_context(
        HOST, defaults(), withheld=host_mode.secret_names()
    )
    env = renderer_env(directory)
    return host_mode._render_template(env, name, ctx, reasons, directory)


def code_lines(script: str) -> list[str]:
    return [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]


def test_the_packaged_render_succeeds_with_every_secret_stripped():
    script = render(ROLE / "templates")
    body = "\n".join(code_lines(script))
    assert "source /usr/lib/homelab/kuma-push-lib.sh" in body
    assert 'kuma_push_token")"' in body
    assert "UPSMON_CONF=/var/lib/homelab-secrets/etc/nut/upsmon.conf" in body
    # Nothing of the Ansible-era branch survives in code.
    for old in (
        "/usr/local/lib/kuma-push-lib.sh",
        "kuma-push.env",
        "UPSMON_CONF=/etc/nut/upsmon.conf",
    ):
        assert old not in body
    assert "{{" not in script and "{%" not in script


def test_the_packaged_render_is_valid_shell(tmp_path):
    path = tmp_path / "ups-secondary-health.sh"
    path.write_text(render(ROLE / "templates"))
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_secret_read_inside_the_packaged_branch_is_flagged(tmp_path):
    (tmp_path / "leaky.sh.j2").write_text(
        "{% if nut_host_watchdog_packaged | bool %}\nTOKEN={{ "
        + SECRET
        + " }}\n{% endif %}\n"
    )
    with pytest.raises(RenderError, match=SECRET):
        render(tmp_path, "leaky.sh.j2")
