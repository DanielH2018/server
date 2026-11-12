"""The UPS watchdog, rendered the way a host package renders it.

The `nut-secondary` component renders its template in host mode, where no secret exists. These
tests render the template that way for daniel-server directly, so the script is known to
render, to read no secret and to be valid shell, whatever the component's file entry does with
it.

Run: uv run pytest hosts/lib/tests/test_nut_secondary_packaged_watchdog.py
"""

import subprocess

import pytest

from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError, renderer_env

COMPONENT = REPO / "hosts" / "components" / "nut-secondary"
WATCHDOG = "ups-secondary-health.sh.j2"
HOST = "daniel-server"
# A SOPS key the packaged render must not reach.
SECRET = "ups_secondary_push_token"


def defaults() -> dict:
    return yaml_fast.safe_load((COMPONENT / "defaults" / "main.yml").read_text())


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
    script = render(COMPONENT / "templates")
    body = "\n".join(code_lines(script))
    assert "UPSMON_CONF=/var/lib/homelab-secrets/etc/nut/upsmon.conf" in body
    # No Ansible-era path and no Kuma push: no library, no credential, no call.
    for old in (
        "UPSMON_CONF=/etc/nut/upsmon.conf",
        "kuma-push-lib",
        "kuma_push",
        "CREDENTIALS_DIRECTORY",
    ):
        assert old not in body
    assert "{{" not in script and "{%" not in script


def test_the_packaged_render_is_valid_shell(tmp_path):
    path = tmp_path / "ups-secondary-health.sh"
    path.write_text(render(COMPONENT / "templates"))
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_a_secret_read_in_the_watchdog_render_is_flagged(tmp_path):
    (tmp_path / "leaky.sh.j2").write_text("TOKEN={{ " + SECRET + " }}\n")
    with pytest.raises(RenderError, match=SECRET):
        render(tmp_path, "leaky.sh.j2")
