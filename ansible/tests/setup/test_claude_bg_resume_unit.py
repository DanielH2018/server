"""The boot-time resume unit must outlive its own run.

claude-bg-resume.service starts the Claude background supervisor as its child, so every session
the supervisor hosts sits in this unit's cgroup. Under the default KillMode a oneshot that
finishes or stops takes all of them down, and that failure would read as "the sessions did not
resume".

Run: uv run pytest ansible/tests/setup/test_claude_bg_resume_unit.py
"""

import re

from _setup_render import render_component_text

COMPONENT = "claude-code"
UNIT = "claude-bg-resume.service.j2"


def directive(unit_text: str, key: str) -> list[str]:
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def render(**overrides: object) -> str:
    return render_component_text(COMPONENT, UNIT, overrides)


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
