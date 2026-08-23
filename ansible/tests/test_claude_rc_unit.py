#!/usr/bin/env python3
"""The claude-rc systemd unit must carry the flags and env that keep it from failing silently.

claude-rc.service supervises `claude rc`, the Remote Control host that lets sessions be
created from a phone. Two of its lines exist purely to defeat failures that produce no error:

1. `--spawn=` must be passed explicitly. With it omitted, Claude Code builds a readline
   interface on stdin to ask which spawn mode to use (guarded on `process.stdin.isTTY`).
   Under systemd there is no stdin to answer it, so the host sits there having never
   connected — a hang, not a crash, so `Restart=always` never fires and the unit reads
   `active` the whole time.

2. `PATH` must name `/usr/local/bin`. systemd supplies a minimal PATH exactly as cron does,
   and this is the same trap already recorded for the host crons: without it a spawned
   session loses kubectl and uv, then reports an EMPTY CLUSTER rather than failing.

Both are invisible at deploy time — Ansible reports the template as applied either way — and
both surface only as a session that hangs or quietly sees nothing. That is the class this
repo escalates from a comment into a check.

`MemoryMax=` is asserted absent for a different reason: systemd applies a memory cap to the
whole cgroup, so one runaway session would take the OOM kill for the host and every other
session with it.

Run: uv run pytest ansible/tests/test_claude_rc_unit.py
"""

import re
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[1]
TEMPLATES = ANSIBLE / "roles" / "setup" / "claude_code" / "templates"
DEFAULTS = ANSIBLE / "roles" / "setup" / "claude_code" / "defaults" / "main.yml"
UNIT = TEMPLATES / "claude-rc.service.j2"


@pytest.fixture(scope="module")
def unit() -> str:
    assert UNIT.exists(), f"{UNIT} is missing — the Remote Control host unit is gone"
    return UNIT.read_text()


def directive(unit_text: str, key: str) -> list[str]:
    """Every value assigned to `key`, with systemd's backslash continuations folded in.

    Read off the template rather than a rendered unit: the point is that the flags survive
    edits to the template, and the Jinja vars are all plain scalars.
    """
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def test_execstart_passes_spawn_mode_explicitly(unit: str) -> None:
    """Omitting --spawn makes the host prompt on a stdin systemd does not give it."""
    exec_starts = directive(unit, "ExecStart")
    assert len(exec_starts) == 1, f"expected exactly one ExecStart, got {exec_starts}"
    assert "--spawn=" in exec_starts[0], (
        "ExecStart must pass --spawn= explicitly. Without it the host reads a spawn-mode "
        "answer from stdin and hangs under systemd, while the unit still reads active."
    )


def test_execstart_pins_permission_mode(unit: str) -> None:
    """An unpinned permission mode silently inherits whatever the default becomes."""
    assert "--permission-mode" in directive(unit, "ExecStart")[0], (
        "ExecStart must pass --permission-mode; sessions reachable from a phone should not "
        "inherit a default that can change under them."
    )


def test_path_includes_usr_local_bin(unit: str) -> None:
    """Without /usr/local/bin a spawned session reports an empty cluster instead of failing."""
    paths = [v for v in directive(unit, "Environment") if v.startswith("PATH=")]
    assert paths, (
        "the unit must set Environment=PATH= — systemd's default omits /usr/local/bin"
    )
    assert "/usr/local/bin" in paths[0], (
        f"PATH must include /usr/local/bin (kubectl, uv); got {paths[0]}"
    )


def test_home_is_set_to_the_real_user(unit: str) -> None:
    """Claude Code is a per-user install and reads its OAuth token from ~/.claude."""
    homes = [v for v in directive(unit, "Environment") if v.startswith("HOME=")]
    assert homes, (
        "the unit must set Environment=HOME= or Claude Code reads the wrong home"
    )
    assert "{{ sys_user }}" in homes[0], (
        f"HOME must be the sys_user's home; got {homes[0]}"
    )


def test_no_memory_max(unit: str) -> None:
    """A cgroup-wide cap kills every session at once, not the one that overran."""
    assert not directive(unit, "MemoryMax"), (
        "MemoryMax applies to the whole cgroup, so one runaway session would OOM-kill the "
        "host and every other session. Bound memory with claude_code_rc_capacity instead."
    )


def test_alert_unit_is_the_onfailure_target(unit: str) -> None:
    assert (TEMPLATES / "claude-rc-alert.service.j2").exists(), (
        "the alert unit is missing"
    )
    assert "OnFailure=claude-rc-alert.service" in unit, (
        "claude-rc.service must page on failure via OnFailure=claude-rc-alert.service"
    )


def test_restart_timer_does_not_start_a_stopped_host(unit: str) -> None:
    """try-restart, not restart: the weekly update restart must never start a stopped host."""
    restart_unit = TEMPLATES / "claude-rc-restart.service.j2"
    assert restart_unit.exists(), "the weekly restart unit is missing"
    exec_start = directive(restart_unit.read_text(), "ExecStart")[0]
    assert "try-restart" in exec_start, (
        "the restart unit must use `systemctl try-restart`; plain `restart` would start a "
        f"host that claude_code_rc_enabled deliberately keeps stopped. Got: {exec_start}"
    )


def test_ships_disabled_by_default() -> None:
    """The host is installed but not started until the trust dialog is proven answerable."""
    text = DEFAULTS.read_text()
    match = re.search(r"^claude_code_rc_enabled:\s*(\S+)", text, re.M)
    assert match, "claude_code_rc_enabled is missing from the role defaults"
    assert match.group(1) == "false", (
        "claude_code_rc_enabled must default to false. Ansible cannot verify that a "
        "phone-spawned worktree clears the workspace-trust dialog, so enabling it is a "
        "deliberate act after that is checked by hand."
    )
