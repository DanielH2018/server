#!/usr/bin/env python3
"""Every process boundary the agent crosses, as one injectable object.

A test replaces one field of `AgentTools` and never a module attribute, which is what
`tests/repo/test_module_length_ratchet.py` requires of a module under test — a
`monkeypatch.setattr` on the entry module would pin its name into the test.

**This module names `renovate_agent` and `run_worktree` nowhere**, at import time or later,
which is what makes it the leaf both of them import.

Stdlib only, like the rest of the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from host_lib import discord_post, flush_discord_spool
from host_lib import log as host_log


def log(msg: str) -> None:
    host_log(msg, tag="renovate-agent")


def read_file(path: str) -> str:
    """`path`'s text, or '' when it cannot be opened. An undecodable byte reads as U+FFFD."""
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def run(
    argv: list[str],
    cwd: str | None = None,
    timeout: int = 120,
    stdin_text: str | None = None,
) -> tuple[int, str]:
    """Run `argv`, returning (returncode, stdout+stderr). Never raises on a non-zero exit.

    `stdin_text`, when given, is the child's stdin; otherwise stdin is /dev/null. A secret goes
    there and never in `argv`: /proc on these hosts has no `hidepid`, so any local user can read
    a running child's command line.
    """
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL if stdin_text is None else None,
            input=stdin_text,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


@dataclass(frozen=True)
class AgentTools:
    """The boundaries the worktree, census, crash-report and spool-flush paths cross.

    The defaults are production, so `main()` passes nothing.

    `open_prs` and `run_session` stay out: their argv is what the suite asserts on, and a field
    there would replace the builder rather than the process. `open_prs` runs its `gh` through
    `run` instead, so a fake answers the census without hiding the argv.
    """

    run: Callable[..., tuple[int, str]] = run
    discord_post: Callable[..., bool] = discord_post
    flush_discord_spool: Callable[..., bool] = flush_discord_spool
    rmtree: Callable[..., None] = shutil.rmtree
    read_file: Callable[[str], str] = read_file


TOOLS = AgentTools()


@dataclass(frozen=True)
class UnitEnd:
    """How a oneshot unit started with `start_unit` ended.

    Attributes:
        result: systemd's `Result` (`success`, `exit-code`, `timeout`, …), or `deadline` when the
            caller's wait ran out first; the unit then keeps running on its own.
        status: the main process's exit status, or -1 when it is unknown.
    """

    result: str
    status: int


def start_unit(unit: str, tools: AgentTools, timeout: int) -> UnitEnd:
    """Start the oneshot `unit`, wait up to `timeout` seconds for it to end, and say how it ended.

    Polkit decides whether this process's user may start `unit`. `systemctl start` blocks until a
    oneshot unit finishes, so `Result` is read after it returns, never while it runs.
    """
    rc, _ = tools.run(["systemctl", "start", unit], timeout=timeout)
    if rc == 124:
        return UnitEnd("deadline", -1)
    _, out = tools.run(
        ["systemctl", "show", "-p", "Result", "-p", "ExecMainStatus", unit]
    )
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    status = props.get("ExecMainStatus", "").strip()
    return UnitEnd(
        props.get("Result", "").strip(), int(status) if status.isdigit() else -1
    )
