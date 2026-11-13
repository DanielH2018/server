#!/usr/bin/env python3
"""A Claude agent's own Unix user reaches nothing of the operator's that it was not handed.

`hosts/components/claude-code` builds the user for the operator's own sessions
(`claude_code_agent_user`). The user exists so a session cannot read
the age key, which decrypts the become password, nor the operator's gh token and ssh keys. Each
test pins one leg of that. A leg that slips fails no deploy: the user still works, only with
more reach than the design gives it.

Run: uv run pytest hosts/tests/test_agent_user.py
"""

import re

from _helpers import ANSIBLE
from _setup_render import render_component_text
from renderer.renderer_lib import host as host_mode


def test_the_packaged_claude_agent_joins_only_the_holder_journal_ssh_and_socket_groups() -> (
    None
):
    """`usermod --groups` sets the whole list, so an added group has to be written into this one."""
    setup = host_mode.render_host("daniel-box").files[
        "/usr/lib/homelab/claude-agent-setup"
    ][0]
    found = re.findall(rb"^groups=\((.*)\)$", setup, re.M)
    assert len(found) == 1
    assert set(found[0].split()) <= {
        b"worktree-holders",
        b"homelab-rootq",
        b"systemd-journal",
        b"ssh-users",
    }
    assert b"sudo" not in found[0]


def test_the_login_profile_points_the_hook_shim_at_the_agents_own_clone_and_uv() -> (
    None
):
    """The shim defaults to the operator's checkout, which this user cannot cd into."""
    profile = render_component_text(
        "claude-code",
        "agent-user-profile.j2",
        {
            "claude_code_agent_user_home": "/srv/agent",
            "claude_code_agent_user_clone_dir": "/srv/clone",
        },
    )
    assert "\nexport RUN_HOOK_PROJECT_DIR=/srv/clone\n" in profile
    assert "\nexport RUN_HOOK_UV=/srv/agent/.local/bin/uv\n" in profile
    assert '\nPATH="$HOME/.local/bin:$PATH"\n' in profile
    shim = (ANSIBLE.parent / ".claude" / "hooks" / "run-hook.sh").read_text()
    assert "${RUN_HOOK_PROJECT_DIR:-" in shim and "${RUN_HOOK_UV:-" in shim, (
        "the hook shim no longer reads the overrides this profile sets"
    )
