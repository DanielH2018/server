#!/usr/bin/env python3
"""Every process boundary the agent crosses, as one injectable object.

A test replaces one field of `AgentTools` and never a module attribute, which is what
`ansible/tests/repo/test_module_length_ratchet.py` requires of a module under test — a
`monkeypatch.setattr` on the entry module would pin its name into the test.

**This module names `renovate_agent` and `run_worktree` nowhere**, at import time or later,
which is what makes it the leaf both of them import. Same shape and same reasoning as
`gitops_deploy`'s `deploy_toolbox.DeployTools`.

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
from host_lib import discord_post


def log(msg: str) -> None:
    print(f"[renovate-agent] {msg}", flush=True)


def read_file(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def run(argv: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str]:
    """Run `argv`, returning (returncode, stdout+stderr). Never raises on a non-zero exit."""
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


@dataclass(frozen=True)
class AgentTools:
    """The four boundaries the worktree, census and crash-report paths cross.

    The defaults are production, so `main()` passes nothing.

    `open_prs` and `run_session` stay out: their argv is what the suite asserts on, and a field
    there would replace the builder rather than the process. `open_prs` runs its `gh` through
    `run` instead, so a fake answers the census without hiding the argv.
    """

    run: Callable[..., tuple[int, str]] = run
    discord_post: Callable[..., bool] = discord_post
    rmtree: Callable[..., None] = shutil.rmtree
    read_file: Callable[[str], str] = read_file


TOOLS = AgentTools()
