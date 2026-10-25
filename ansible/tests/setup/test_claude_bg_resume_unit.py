"""The boot-time resume unit must outlive its own run and must never resume on an apply.

claude-bg-resume.service starts the Claude background supervisor as its child, so every session
the supervisor hosts sits in this unit's cgroup. Under the default KillMode a oneshot that
finishes or stops takes all of them down, and that failure would read as "the sessions did not
resume". The other assertions keep the unit's two directions honest: armed, it is enabled for the
next boot and never started by the role; disarmed, it is stopped, disabled and removed.

Run: uv run pytest ansible/tests/setup/test_claude_bg_resume_unit.py
"""

import re

from _helpers import ANSIBLE
from _setup_render import render_setup_text

ROLE = "claude_code"
UNIT = "claude-bg-resume.service.j2"
TASKS = ANSIBLE / "roles" / "setup" / ROLE / "tasks" / "main.yml"


def directive(unit_text: str, key: str) -> list[str]:
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def render(**overrides: object) -> str:
    return render_setup_text(ROLE, UNIT, overrides)


def task_block(name: str) -> str:
    match = re.search(
        rf"^- name: {re.escape(name)}\n(.*?)(?=^- name:|\Z)",
        TASKS.read_text(),
        re.S | re.M,
    )
    assert match, f"no task named {name!r} in {TASKS}"
    return match.group(1)


def test_the_sessions_it_starts_survive_the_unit() -> None:
    unit = render()
    assert directive(unit, "KillMode") == ["process"], (
        "KillMode must be process: the supervisor the script starts lives in this cgroup, and "
        "the default control-group kill ends every background session when the unit stops"
    )
    assert directive(unit, "RemainAfterExit") == ["yes"], (
        "a oneshot that goes inactive after its script returns kills what it started"
    )


def test_respawned_sessions_get_a_path_with_kubectl_and_uv() -> None:
    path = [v for v in directive(render(), "Environment") if v.startswith("PATH=")]
    assert len(path) == 1 and "/usr/local/bin" in path[0].split(":"), (
        "PATH must name /usr/local/bin, or a respawned session loses kubectl and uv and "
        f"reports an empty cluster. Got: {path}"
    )


def test_it_runs_as_the_configured_user_from_that_users_home() -> None:
    moved = render(claude_code_bg_resume_user="someone")
    assert directive(moved, "User") == ["someone"]
    assert "HOME=/home/someone" in directive(moved, "Environment")
    assert (
        "--claude /home/someone/.local/bin/claude" in directive(moved, "ExecStart")[0]
    )


def test_the_role_enables_it_for_the_next_boot_and_never_starts_it() -> None:
    body = task_block("Enable the background-session resume unit for the next boot")
    assert "enabled: true" in body
    assert "state:" not in body, (
        "starting or restarting the unit on an apply would respawn sessions mid-day"
    )
    assert "when: claude_code_bg_resume_enabled" in body


def test_the_flag_also_takes_it_out() -> None:
    body = task_block(
        "Stop and disable the background-session resume unit when disarmed"
    )
    assert "enabled: false" in body and "state: stopped" in body
    assert "not claude_code_bg_resume_enabled" in body
    removed = task_block(
        "Remove the background-session resume unit and script when disarmed"
    )
    assert "/etc/systemd/system/claude-bg-resume.service" in removed
    assert "claude_code_bg_resume_dir" in removed
