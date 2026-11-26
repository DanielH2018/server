#!/usr/bin/env python3
"""The unattended session units' bounds: what a session may write, and how much (guardrails B2-3).

Nothing a session writes reaches a later run, and the run is capped. Both
session units, triage's and Renovate's, get these from systemd: per-run tmpfs mounts, a read-only
system and cgroup caps. Each case is one session: its component, unit, pins file, user, the
package that declares the user, and the exchange paths it may write.

Run: uv run pytest hosts/tests/host_config/test_unattended_session_bounds.py
"""

import re
from dataclasses import dataclass

import pytest
from _helpers import REPO
from _setup_render import COMPONENTS, render_component_text, role_context
from lib import yaml_fast
from lib.guard_jinja_env import template_env


@dataclass(frozen=True)
class Session:
    component: str
    unit: str
    pins: str
    user: str
    users_from: str
    writable: tuple[str, ...]
    home: str


SESSIONS = [
    Session(
        "claude-code",
        "claude-triage-session.service.j2",
        "claude-triage-pins.j2",
        "claude-triage",
        "hosts/daniel-box/package.yml",
        ("out",),
        "session_home",
    ),
    Session(
        "renovate-agent",
        "homelab-renovate-agent-session.service.j2",
        "renovate-agent-pins.j2",
        "renovate-agent",
        "hosts/components/renovate-agent/component.yml",
        ("requests", "out"),
        "renovate_agent_home",
    ),
]


def directive(text: str, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in re.sub(r"\\\n\s*", " ", text).splitlines()
        if line.startswith(f"{key}=")
    ]


def pin(session: Session, name: str):
    """One value the session's pins file exports."""
    root = COMPONENTS / session.component
    module = (
        template_env(root / "templates")
        .get_template(session.pins)
        .make_module(role_context(root))
    )
    return getattr(module, name)


def home(session: Session) -> str:
    """The session's home: a pin for triage, a component default for Renovate."""
    if session.home == "session_home":
        return pin(session, "session_home")
    defaults = COMPONENTS / session.component / "defaults" / "main.yml"
    return yaml_fast.safe_load(defaults.read_text())[session.home]


def _size_bytes(value: str) -> int:
    return int(value[:-1]) << {"K": 10, "M": 20, "G": 30}[value[-1]]


@pytest.mark.parametrize("session", SESSIONS, ids=lambda s: s.component)
def test_the_session_writes_only_per_run_tmpfs_mounts_and_its_exchange(session) -> None:
    """Every other path is read-only, and each mount is capped."""
    unit = render_component_text(session.component, session.unit)
    assert directive(unit, "ProtectSystem") == ["strict"]
    assert directive(unit, "PrivateTmp") == []
    exchange = pin(session, "exchange_dir")
    assert directive(unit, "ReadWritePaths") == [
        " ".join(f"{exchange}/{name}" for name in session.writable)
    ]
    mounts = dict(m.split(":", 1) for m in directive(unit, "TemporaryFileSystem"))
    assert set(mounts) == {home(session), "/tmp", "/var/tmp", "/dev/shm"}
    sizes = [
        _size_bytes(dict(o.split("=") for o in opts.split(","))["size"])
        for opts in mounts.values()
    ]
    # tmpfs pages are the unit's memory, so a full mount must hit ENOSPC before MemoryMax.
    (memory_max,) = directive(unit, "MemoryMax")
    assert sum(sizes) < _size_bytes(memory_max) // 2
    assert directive(unit, "TasksMax") == [str(pin(session, "tasks_max"))]
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in directive(unit, "Environment")
    # The wall clock's signal, which leaves the session script alive to copy the transcript.
    assert directive(unit, "KillSignal") == ["SIGINT"]


@pytest.mark.parametrize("session", SESSIONS, ids=lambda s: s.component)
def test_the_session_home_mount_belongs_to_the_declared_session_uid(session) -> None:
    """tmpfs takes a numeric uid, so the pin must equal the uid the users arm creates."""
    spec = yaml_fast.safe_load((REPO / session.users_from).read_text())
    (declared,) = [u["uid"] for u in spec["users"] if u["name"] == session.user]
    unit = render_component_text(session.component, session.unit)
    (opts,) = [
        m.split(":", 1)[1]
        for m in directive(unit, "TemporaryFileSystem")
        if m.startswith(home(session) + ":")
    ]
    assert f"uid={declared}" in opts.split(",")
    assert f"gid={declared}" in opts.split(",")
