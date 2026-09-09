#!/usr/bin/env python3
"""daniel-server is a Claude session host with its own cap numbers (spec 2026-09-06 §1).

The caps are the first per-host overrides of the claude_code role; before this every number
came from defaults/main.yml. The render is the same jinja2.Template call the sibling
test_claude_login_slice_caps.py makes, with the host's vars laid over the defaults.
Run: uv run pytest ansible/tests/setup/test_claude_code_on_daniel_server.py
"""

import jinja2

from _helpers import ANSIBLE, HOST_VARS, load_tasks, load_yaml, task_named
from test_claude_fleet_slice_cap import SUB_BOUNDS, to_bytes

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
DEFAULTS = load_yaml(ROLE / "defaults" / "main.yml")
SERVER = load_yaml(HOST_VARS / "daniel-server.yml")
BOX = load_yaml(HOST_VARS / "daniel-box.yml")


def _violating_planes(merged_vars: dict) -> list[str]:
    """The SUB_BOUNDS plane vars in `merged_vars` that exceed the fleet var they sit under.

    Empty means every plane stays within its fleet bound.
    """
    return [
        plane_var
        for plane_var, fleet_var in SUB_BOUNDS.items()
        if to_bytes(merged_vars[plane_var]) > to_bytes(merged_vars[fleet_var])
    ]


def _render(template: str, host_vars: dict) -> str:
    ctx = {"sys_user": "ubuntu", **DEFAULTS, **host_vars}
    text = (ROLE / "templates" / template).read_text()
    return jinja2.Template(text, undefined=jinja2.StrictUndefined).render(ctx)


def test_daniel_server_opts_in_without_the_remote_control_unit():
    assert SERVER["has_claude_code"] is True
    assert SERVER["claude_code_rc_enabled"] is False
    # daniel-box keeps the role default (true); the RC host stays single.
    assert "claude_code_rc_enabled" not in BOX


def test_daniel_server_carries_its_own_fleet_cap():
    """Its unreclaimable footprint is larger than daniel-box's (a KVM guest), so the number differs."""
    assert (
        SERVER["claude_code_fleet_memory_high"]
        != DEFAULTS["claude_code_fleet_memory_high"]
    )
    rendered = _render("fleet-slice-caps.conf.j2", SERVER)
    assert f"MemoryHigh={SERVER['claude_code_fleet_memory_high']}" in rendered
    assert "MemoryHigh=%s" % DEFAULTS["claude_code_fleet_memory_high"] not in rendered


def test_daniel_box_render_is_unchanged_by_the_server_block():
    rendered = _render("fleet-slice-caps.conf.j2", BOX)
    assert "MemoryHigh=%s" % DEFAULTS["claude_code_fleet_memory_high"] in rendered


def test_the_role_enables_linger_idempotently():
    task = task_named(load_tasks(ROLE / "tasks" / "main.yml"), "enable-linger")
    assert task["args"]["creates"] == "/var/lib/systemd/linger/{{ sys_user }}"
    assert task.get("become") is True


def test_daniel_server_planes_stay_under_its_fleet_cap():
    """Its own fleet override (10G/2G) must still bound its own planes (8G/2G).

    test_claude_fleet_slice_cap.py's sibling check reads only defaults/main.yml, so it never
    sees a host_vars override — a future edit dropping daniel-server's fleet cap below one of
    its planes would pass that check green.
    """
    merged = {**DEFAULTS, **SERVER}
    violations = _violating_planes(merged)
    assert not violations, (
        f"{violations} exceed daniel-server's fleet bound "
        f"({SERVER['claude_code_fleet_memory_high']}/{SERVER['claude_code_fleet_swap_max']})"
    )


def test_the_sub_bound_check_rejects_a_lowered_server_fleet_cap():
    """Rejecting half of the pair above: a fleet cap dropped below a plane's must not pass."""
    lowered = {**DEFAULTS, **SERVER, "claude_code_fleet_memory_high": "6G"}
    assert _violating_planes(lowered) == ["claude_code_rc_memory_high"], (
        "lowering claude_code_fleet_memory_high below claude_code_rc_memory_high must be "
        "caught, or the check is not actually reading the override"
    )
