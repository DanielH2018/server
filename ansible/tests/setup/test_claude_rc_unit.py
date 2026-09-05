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

3. `PYTEST_XDIST_AUTO_NUM_WORKERS` must be exported, and both it and `MemoryHigh` must render
   from variables rather than hardcoded numbers. This is the same silent class: on 2026-09-05
   nine concurrent `-n auto` pytest runs resolved to ~144 workers, filled the cgroup and took
   all 8 GB of the system's swap, and the host stalled unreachable for ~30 minutes while the
   unit read `active (running)`. A cap hardcoded in the template would pass a presence check
   while ignoring the default it is supposed to read, so each is tested as a pair: one render
   it must accept, one override it must follow.

Run: uv run pytest ansible/tests/setup/test_claude_rc_unit.py
"""

import re

import pytest
from _helpers import ANSIBLE

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
    # systemd's last assignment of a variable wins, so the effective PATH is paths[-1].
    # Reading paths[0] would pass on a unit whose second PATH= drops /usr/local/bin.
    assert "/usr/local/bin" in paths[-1], (
        f"PATH must include /usr/local/bin (kubectl, uv); got {paths[-1]}"
    )


def test_home_is_set_to_the_real_user(unit: str) -> None:
    """Claude Code is a per-user install and reads its OAuth token from ~/.claude."""
    homes = [v for v in directive(unit, "Environment") if v.startswith("HOME=")]
    assert homes, (
        "the unit must set Environment=HOME= or Claude Code reads the wrong home"
    )
    # Last assignment wins, as with PATH above.
    assert "{{ sys_user }}" in homes[-1], (
        f"HOME must be the sys_user's home; got {homes[-1]}"
    )


def test_no_memory_max(unit: str) -> None:
    """A cgroup-wide cap kills every session at once, not the one that overran."""
    assert not directive(unit, "MemoryMax"), (
        "MemoryMax applies to the whole cgroup, so one runaway session would OOM-kill the "
        "host and every other session. Bound the fan-out with claude_code_rc_pytest_workers "
        "instead — capacity bounds session count, not memory (see this role's CLAUDE.md)."
    )


def test_pytest_fanout_is_capped(unit: str) -> None:
    """`-n auto` resolves per-core per RUN, so concurrent runs multiply it unchecked.

    `addopts` in pyproject.toml carries `-n auto`. On daniel-box's 16 cores that is 16
    workers for one run, and nothing caps how many runs the sessions on this host start at
    once: on 2026-09-05 nine concurrent runs put ~144 workers in the cgroup and took all 8 GB
    of the system's swap. xdist reads PYTEST_XDIST_AUTO_NUM_WORKERS before any CPU detection,
    so setting it on the unit bounds every run a session makes while leaving CI at full width.
    """
    assert re.search(
        r"^claude_code_rc_pytest_workers: *\d+", DEFAULTS.read_text(), re.M
    ), "claude_code_rc_pytest_workers is gone from defaults — the fan-out cap is unset"
    assert "Environment=PYTEST_XDIST_AUTO_NUM_WORKERS=4" in render(unit), (
        "the unit must export PYTEST_XDIST_AUTO_NUM_WORKERS; without it a session's "
        "`-n auto` resolves to one worker per core and concurrent runs multiply it"
    )


def test_pytest_fanout_cap_follows_the_variable(unit: str) -> None:
    """The rejecting half: a hardcoded 4 would pass the test above and ignore the default."""
    rendered = render(unit, claude_code_rc_pytest_workers=1)
    assert "Environment=PYTEST_XDIST_AUTO_NUM_WORKERS=1" in rendered, (
        "PYTEST_XDIST_AUTO_NUM_WORKERS must render from claude_code_rc_pytest_workers"
    )
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS=4" not in rendered, (
        "PYTEST_XDIST_AUTO_NUM_WORKERS is hardcoded to 4 — changing the default would "
        "silently do nothing"
    )


def test_memory_high_follows_the_variable(unit: str) -> None:
    """MemoryHigh throttles the whole cgroup, so its value is an operational decision.

    It was hardcoded at 6G until 2026-09-05. Raising it is a trade against the ~13 GB k3s +
    kubepods baseline on this 28 GB box — the throttle stalls whichever cgroup loses the
    race, and the homelab plane is the one that must not. That belongs in defaults where the
    reasoning sits beside the number, not inline in the template.
    """
    assert re.search(
        r"^claude_code_rc_memory_high: *\S+", DEFAULTS.read_text(), re.M
    ), "claude_code_rc_memory_high is gone from defaults — MemoryHigh has no owner"
    assert "MemoryHigh=8G" in render(unit), "MemoryHigh must render from defaults' 8G"
    rendered = render(unit, claude_code_rc_memory_high="12G")
    assert "MemoryHigh=12G" in rendered and "MemoryHigh=8G" not in rendered, (
        "MemoryHigh is hardcoded — changing claude_code_rc_memory_high would do nothing"
    )


def test_memory_swap_max_bounds_the_cgroups_swap(unit: str) -> None:
    """MemoryHigh throttles rather than caps, and a throttled cgroup's anon pages go to swap.

    With no MemorySwapMax the throttle above therefore has no ceiling: on 2026-09-05 this
    cgroup took all 8 GiB of the host's swap and starved k3s and kubepods.slice while the
    unit read `active (running)`. Issue #1154. Like MemoryHigh, the value is an operational
    trade against the rest of the box, so it renders from defaults rather than the template.
    """
    assert re.search(
        r"^claude_code_rc_memory_swap_max: *\S+", DEFAULTS.read_text(), re.M
    ), (
        "claude_code_rc_memory_swap_max is gone from defaults — MemorySwapMax has no owner"
    )
    assert "MemorySwapMax=2G" in render(unit), (
        "MemorySwapMax must render from defaults' 2G"
    )
    rendered = render(unit, claude_code_rc_memory_swap_max="4G")
    assert "MemorySwapMax=4G" in rendered and "MemorySwapMax=2G" not in rendered, (
        "MemorySwapMax is hardcoded — changing claude_code_rc_memory_swap_max would do nothing"
    )


def test_alert_unit_is_the_onfailure_target(unit: str) -> None:
    assert (TEMPLATES / "claude-rc-alert.service.j2").exists(), (
        "the alert unit is missing"
    )
    assert "OnFailure=claude-rc-alert.service" in unit, (
        "claude-rc.service must page on failure via OnFailure=claude-rc-alert.service"
    )


REAP_ENV = "CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP"
REAP_VAR = "claude_code_rc_disable_bg_shell_pressure_reap"


def render(unit_text: str, **overrides: object) -> str:
    """Render the unit template with plausible scalars, so a Jinja guard is really exercised.

    The other tests here read the raw text, which is enough for a line that is always
    present. It is not enough for a line behind `{% if %}`: raw text cannot tell an armed
    toggle from a dead one.
    """
    import jinja2

    context: dict[str, object] = {
        "sys_user": "ubuntu",
        "claude_code_rc_spawn_mode": "worktree",
        "claude_code_rc_workdir": "/home/ubuntu/server",
        "claude_code_rc_permission_mode": "auto",
        "claude_code_rc_capacity": 10,
        "claude_code_rc_pytest_workers": 4,
        "claude_code_rc_memory_high": "8G",
        "claude_code_rc_memory_swap_max": "2G",
        REAP_VAR: True,
    }
    context.update(overrides)
    return jinja2.Template(unit_text, undefined=jinja2.StrictUndefined).render(context)


def test_bg_shell_pressure_reap_is_disabled_by_default(unit: str) -> None:
    """A reaped background task is not a failed one, and it kills a landing mid-flight.

    Claude Code kills every running backgrounded Bash task on a Node `memoryPressure` event.
    The `land-after-merge` skill runs `land.sh --arm-merge` that way, so a reap between the
    arm and the CI wait merges the PR and deploys nothing — issue #1096.
    """
    assert re.search(rf"^{REAP_VAR}: *true\b", DEFAULTS.read_text(), re.M), (
        f"{REAP_VAR} must default to true in the role defaults, or an unattended landing "
        "can be killed after its auto-merge is armed and never followed through"
    )
    assert f"Environment={REAP_ENV}=1" in render(unit), (
        f"the unit must set Environment={REAP_ENV}=1 when {REAP_VAR} is true"
    )


def test_the_reap_toggle_gives_the_reaper_back(unit: str) -> None:
    """The rejecting half: setting the var false must actually remove the line.

    A guard keyed on a variable nothing sets renders the same both ways, which reads exactly
    like a working toggle from the passing side alone.
    """
    assert REAP_ENV not in render(unit, **{REAP_VAR: False}), (
        f"{REAP_VAR}: false must drop the Environment line; the reaper is upstream's "
        "protection against a runaway background shell and has to be recoverable"
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


def test_enable_flag_also_turns_the_host_off() -> None:
    """The flag must drive both directions, not just the way in.

    A var that only enables leaves no rollback: the host keeps running and keeps starting at
    boot after someone sets it back to false. Both systemd tasks therefore have to read the
    flag for `enabled:` AND for `state:`.
    """
    tasks = (
        ANSIBLE / "roles" / "setup" / "claude_code" / "tasks" / "main.yml"
    ).read_text()
    assert re.search(r"^claude_code_rc_enabled:", DEFAULTS.read_text(), re.M), (
        "claude_code_rc_enabled is missing from the role defaults"
    )
    for unit_name in ("claude-rc.service", "claude-rc-restart.timer"):
        block = re.search(
            rf"name: {re.escape(unit_name)}\n(.*?)(?=\n- name:|\Z)", tasks, re.S
        )
        assert block, f"no systemd task manages {unit_name}"
        body = block.group(1)
        assert 'enabled: "{{ claude_code_rc_enabled }}"' in body, (
            f"{unit_name} must take `enabled:` from claude_code_rc_enabled"
        )
        assert "if claude_code_rc_enabled else 'stopped'" in body, (
            f"{unit_name} must be STOPPED when claude_code_rc_enabled is false, not merely "
            "left disabled — otherwise the flag has no rollback."
        )
