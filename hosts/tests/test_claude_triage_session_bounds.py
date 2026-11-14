#!/usr/bin/env python3
"""The triage session unit's bounds: what the session may write, and how much (guardrails B2-3).

dotfiles#967's I2 says nothing a session writes reaches a later run, and I8 caps the run. The
session unit gets both from systemd: per-run tmpfs mounts, a read-only system and cgroup caps.
docs/claude-agent-user.md, "Alert-triggered triage", has the design.

Run: uv run pytest hosts/tests/test_claude_triage_session_bounds.py
"""

import re

from _helpers import REPO
from _setup_render import CLAUDE_CODE, render_component_text, role_context
from lib import yaml_fast
from lib.guard_jinja_env import template_env

ROLE = CLAUDE_CODE
SESSION_UNIT = "claude-triage-session.service.j2"
SESSION_USER = "claude-triage"


def directive(text: str, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in re.sub(r"\\\n\s*", " ", text).splitlines()
        if line.startswith(f"{key}=")
    ]


def pin(name: str):
    """One value claude-triage-pins.j2 exports."""
    module = (
        template_env(ROLE / "templates")
        .get_template("claude-triage-pins.j2")
        .make_module(role_context(ROLE))
    )
    return getattr(module, name)


def _size_bytes(value: str) -> int:
    return int(value[:-1]) << {"K": 10, "M": 20, "G": 30}[value[-1]]


def test_the_session_writes_only_per_run_tmpfs_mounts_and_out() -> None:
    """dotfiles#967's I2 and I8: every other path is read-only, and each mount is capped."""
    session = render_component_text("claude-code", SESSION_UNIT)
    assert directive(session, "ProtectSystem") == ["strict"]
    assert directive(session, "PrivateTmp") == []
    assert directive(session, "ReadWritePaths") == [f"{pin('exchange_dir')}/out"]
    mounts = dict(m.split(":", 1) for m in directive(session, "TemporaryFileSystem"))
    assert set(mounts) == {
        pin("session_home"),
        "/tmp",
        "/var/tmp",
        "/dev/shm",
    }
    sizes = [
        _size_bytes(dict(o.split("=") for o in opts.split(","))["size"])
        for opts in mounts.values()
    ]
    # tmpfs pages are the unit's memory, so a full mount must hit ENOSPC before MemoryMax.
    (memory_max,) = directive(session, "MemoryMax")
    assert sum(sizes) < _size_bytes(memory_max) // 2
    assert directive(session, "TasksMax") == [str(pin("tasks_max"))]
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in directive(session, "Environment")


def test_the_session_home_mount_belongs_to_the_declared_session_uid() -> None:
    """tmpfs takes a numeric uid, so the pin must equal the uid the users arm creates."""
    package = yaml_fast.safe_load(
        (REPO / "hosts" / "daniel-box" / "package.yml").read_text()
    )
    (declared,) = [u["uid"] for u in package["users"] if u["name"] == SESSION_USER]
    session = render_component_text("claude-code", SESSION_UNIT)
    home = pin("session_home")
    (opts,) = [
        m.split(":", 1)[1]
        for m in directive(session, "TemporaryFileSystem")
        if m.startswith(home + ":")
    ]
    assert f"uid={declared}" in opts.split(",")
    assert f"gid={declared}" in opts.split(",")
