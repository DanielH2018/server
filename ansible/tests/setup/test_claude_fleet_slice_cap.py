#!/usr/bin/env python3
"""The Claude fleet's memory bound must be ONE number on ONE cgroup, not two that look like one.

Issue #1264: claude-rc.service and user-<uid>.slice both rendered
`claude_code_rc_memory_high`. Sharing a variable is not sharing a cap — they are cgroup
siblings under different parents (system.slice and user.slice), so neither saw the other's
usage and the fleet's real throttle point was the SUM: 16G of anon plus 4G of swap on a
28 GiB box that also runs k3s and Longhorn.

The fix nests both planes under one parent slice that carries the single derived bound
(`templates/fleet-slice-caps.conf.j2` on user.slice, plus `Slice=` on the RC unit), leaving
the per-plane caps as sub-bounds. `user-<uid>.slice` itself cannot be reparented —
systemd.slice(5) derives a slice's parent from its NAME — so the unit we do control is the
one that moves. This module pins the parts of that shape a future edit could quietly undo:

1. The parent's caps render from the FLEET variables, not from a plane's.
2. The RC unit is placed in that parent slice, and stops being placed there when the shared
   parent is turned off (the rollback direction).
3. Every per-plane cap is within the fleet bound — a sub-bound above its parent's can never
   be reached, so it would silently mean the parent's number instead.
4. The drop-in has a removal task, so the shared parent has a way out as well as a way in.

Each check is a pair per this repo's "a new check ships with a proof it can go RED" rule: one
input it must accept and one it must reject.

Run: uv run pytest ansible/tests/setup/test_claude_fleet_slice_cap.py
"""

import re

import jinja2
import pytest
from _helpers import ANSIBLE

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
TEMPLATES = ROLE / "templates"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"

FLEET_CONF = TEMPLATES / "fleet-slice-caps.conf.j2"
RC_UNIT = TEMPLATES / "claude-rc.service.j2"

# The vars this module reads. Asserted present before anything is compared, so a rename that
# empties the parse fails here rather than passing an all() over nothing.
REQUIRED_VARS = frozenset(
    {
        "claude_code_fleet_caps_enabled",
        "claude_code_fleet_slice",
        "claude_code_fleet_memory_high",
        "claude_code_fleet_swap_max",
        "claude_code_rc_memory_high",
        "claude_code_rc_memory_swap_max",
    }
)

CONTEXT: dict[str, object] = {
    "sys_user": "ubuntu",
    "claude_code_login_uid": 1000,
    "claude_code_rc_spawn_mode": "worktree",
    "claude_code_rc_workdir": "/home/ubuntu/server",
    "claude_code_rc_permission_mode": "auto",
    "claude_code_rc_capacity": 10,
    "claude_code_rc_pytest_workers": 4,
    "claude_code_rc_memory_high": "8G",
    "claude_code_rc_memory_swap_max": "2G",
    "claude_code_rc_disable_bg_shell_pressure_reap": True,
    "claude_code_fleet_caps_enabled": True,
    "claude_code_fleet_slice": "user.slice",
    "claude_code_fleet_memory_high": "12G",
    "claude_code_fleet_swap_max": "2G",
}

# The per-plane cap -> the fleet cap it must sit under.
SUB_BOUNDS = {
    "claude_code_rc_memory_high": "claude_code_fleet_memory_high",
    "claude_code_rc_memory_swap_max": "claude_code_fleet_swap_max",
}

_SUFFIXES = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def to_bytes(value: object) -> int:
    """Parse a systemd memory size ("12G", "512M", "8589934592") into bytes.

    systemd's own suffixes are powers of 1024 (systemd.syntax: K/M/G/T are binary), so this
    reads 12G as 12 GiB, matching what the kernel writes into memory.high.
    """
    text = str(value).strip()
    match = re.fullmatch(r"(\d+)([KMGT])?", text)
    assert match, f"{value!r} is not a systemd memory size"
    return int(match.group(1)) * _SUFFIXES.get(match.group(2) or "", 1)


def render(template_text: str, **overrides: object) -> str:
    return jinja2.Template(template_text, undefined=jinja2.StrictUndefined).render(
        {**CONTEXT, **overrides}
    )


@pytest.fixture(scope="module")
def defaults() -> dict[str, object]:
    from lib import yaml_fast

    parsed = yaml_fast.safe_load(DEFAULTS.read_text())
    missing = REQUIRED_VARS - set(parsed)
    assert not missing, (
        f"defaults/main.yml no longer defines {sorted(missing)} — the fleet bound has no "
        "owner, and every comparison below would pass over an empty set"
    )
    return parsed


@pytest.fixture(scope="module")
def fleet_conf() -> str:
    assert FLEET_CONF.exists(), (
        f"{FLEET_CONF} is missing — the two planes have no shared parent cap, which is the "
        "16G-instead-of-8G state issue #1264 describes"
    )
    return FLEET_CONF.read_text()


@pytest.fixture(scope="module")
def rc_unit() -> str:
    return RC_UNIT.read_text()


def test_parent_cap_renders_from_the_fleet_variables(fleet_conf: str) -> None:
    """The accepting half plus its rejection: a hardcoded 12G would pass a presence check.

    The number belongs in defaults where the derivation against the box's headroom sits
    beside it, not inline in a drop-in.
    """
    rendered = render(fleet_conf)
    assert "MemoryHigh=12G" in rendered, "MemoryHigh must render from defaults' 12G"
    assert "MemorySwapMax=2G" in rendered, "MemorySwapMax must render from defaults' 2G"

    raised = render(
        fleet_conf,
        claude_code_fleet_memory_high="16G",
        claude_code_fleet_swap_max="4G",
    )
    assert "MemoryHigh=16G" in raised and "MemoryHigh=12G" not in raised, (
        "MemoryHigh in fleet-slice-caps.conf.j2 is hardcoded — changing "
        "claude_code_fleet_memory_high would do nothing"
    )
    assert "MemorySwapMax=4G" in raised and "MemorySwapMax=2G" not in raised, (
        "MemorySwapMax in fleet-slice-caps.conf.j2 is hardcoded — changing "
        "claude_code_fleet_swap_max would do nothing"
    )


def test_parent_cap_does_not_render_a_plane_variable(fleet_conf: str) -> None:
    """The rejecting half of the pair above, aimed at the actual defect this closes.

    Rendering claude_code_rc_memory_high here would reproduce #1264 with an extra cgroup:
    a parent whose ceiling equals one plane's, so the pair still throttles at their sum.
    """
    rendered = render(fleet_conf, claude_code_rc_memory_high="99G")
    assert "99G" not in rendered, (
        "the parent slice must carry the FLEET bound, not a plane's — a parent equal to one "
        "plane's cap leaves the fleet's real bound at the sum again"
    )


def test_parent_cap_sets_no_memory_max(fleet_conf: str) -> None:
    """MemoryMax on the parent would OOM-kill every login session and the RC host at once,
    including the SSH connection running the deploy. Same decision as both planes."""
    assert "MemoryMax=" not in render(fleet_conf), (
        "MemoryMax on the shared parent kills the whole fleet on one runaway session; "
        "MemoryHigh throttles instead"
    )


def test_rc_unit_is_placed_in_the_fleet_slice(rc_unit: str) -> None:
    """Without Slice=, the RC unit stays in system.slice and the parent cap covers one plane.

    The parent slice is the only half of the fix that cannot be inferred from the drop-in:
    user-<uid>.slice is already under user.slice by name, and claude-rc.service is not.
    """
    assert "Slice=user.slice" in render(rc_unit), (
        "claude-rc.service must be placed in the fleet slice, or it stays in system.slice "
        "and shares no parent with the login-session plane"
    )
    moved = render(rc_unit, claude_code_fleet_slice="claude-fleet.slice")
    assert "Slice=claude-fleet.slice" in moved and "Slice=user.slice" not in moved, (
        "Slice= is hardcoded — changing claude_code_fleet_slice would do nothing"
    )


def test_rc_unit_leaves_the_fleet_slice_when_the_shared_parent_is_disabled(
    rc_unit: str,
) -> None:
    """The way out. claude_code_fleet_caps_enabled: false removes the drop-in, so the unit
    must stop naming the slice as well — otherwise it sits in an uncapped user.slice and the
    rollback silently drops its own plane's parent."""
    rendered = render(rc_unit, claude_code_fleet_caps_enabled=False)
    assert not re.search(r"^Slice=", rendered, re.M), (
        "with claude_code_fleet_caps_enabled false the unit must fall back to the default "
        "system.slice, not stay in a slice whose cap has just been removed"
    )


def test_every_plane_cap_is_within_the_fleet_bound(defaults: dict[str, object]) -> None:
    """A sub-bound above its parent's can never be reached, so it means the parent's number.

    That is how #1264 read in reverse: two 8G planes under no parent at all. This is the
    arithmetic to re-check when either number moves.
    """
    for plane_var, fleet_var in SUB_BOUNDS.items():
        plane = to_bytes(defaults[plane_var])
        fleet = to_bytes(defaults[fleet_var])
        assert plane <= fleet, (
            f"{plane_var}={defaults[plane_var]} exceeds {fleet_var}="
            f"{defaults[fleet_var]} — a plane cannot be allowed more than the whole fleet"
        )


def test_the_sub_bound_check_rejects_a_plane_larger_than_the_fleet() -> None:
    """Rejecting half of the check above: prove the comparison can fail, and that the size
    parser is not silently reading every value as equal."""
    assert to_bytes("8G") <= to_bytes("12G")
    assert not to_bytes("16G") <= to_bytes("12G")
    assert to_bytes("12G") == 12 * 1024**3
    assert to_bytes("2G") == to_bytes("2048M")


def test_tasks_render_and_remove_the_parent_drop_in() -> None:
    """Both directions, in the tasks that actually run: written when enabled, deleted when not.

    A drop-in with no removal task is a one-way door — flipping the var back would leave the
    shared ceiling in place with nothing in the repo saying so.
    """
    tasks = TASKS.read_text()
    assert "src: fleet-slice-caps.conf.j2" in tasks, (
        "no task renders the fleet slice drop-in"
    )
    assert (
        'dest: "/etc/systemd/system/{{ claude_code_fleet_slice }}.d/claude-fleet-caps.conf"'
        in tasks
    ), "the drop-in must land in the fleet slice's own drop-in directory"
    assert "when: not claude_code_fleet_caps_enabled" in tasks, (
        "nothing removes the fleet drop-in when claude_code_fleet_caps_enabled is false"
    )
    assert re.search(
        r"state: absent\n\s+become: true\n\s+when: not claude_code_fleet", tasks
    ), "the removal task must be the file:-absent one guarded on the fleet toggle"


def test_parent_drop_in_is_stamped_for_drift_checking() -> None:
    """An unstamped template can sit edited-but-undeployed with every repo-side check green —
    the gap the role's stamp_render list exists to close (2026-08-24 review M-4)."""
    tasks = TASKS.read_text()
    assert (
        "ansible/roles/setup/claude_code/templates/fleet-slice-caps.conf.j2" in tasks
    ), "fleet-slice-caps.conf.j2 is missing from stamp_render_templates"
