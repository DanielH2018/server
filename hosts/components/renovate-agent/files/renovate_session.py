#!/usr/bin/env python3
"""The Renovate agent's session, run in its own unit that the runner starts.

`homelab-renovate-agent-session.service` runs this as `renovate_agent_user` (guardrails plan B2r). It
checks the run worktree can be recreated, recreates it, runs the headless Claude session in it,
and writes what the session printed to `OUTPUT_FILE` under the state directory. A worktree that
still holds work is left alone: the reason goes to `BLOCKED_FILE` and no session starts. The
runner reads both files after the unit ends.

The session's output never reaches the journal: it is captured here and written to a file, so
text a session controls cannot become a syslog line that loki-homelab's rules read
(dotfiles#967's I3 and I6). The unit's `TimeoutStartSec` is the session's wall clock; systemd
stops the whole unit at it, and the runner reads `Result=timeout`.

Usage: renovate_session.py   (takes no arguments; -h/--help prints this text)
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_toolbox import TOOLS, AgentTools, log, read_file
from host_lib import atomic_write, install_excepthook, parse_env_file
from run_requests import REQUESTS_DIR
from run_worktree import prepare_worktree, worktree_is_reusable

CONFIG = os.environ.get("RENOVATE_AGENT_CONFIG", "/etc/renovate-agent/config.env")
OUTPUT_FILE = "session.out"
BLOCKED_FILE = "session.blocked"
# The unit's EnvironmentFile: the session's GitHub token and Claude credential, root 0600.
SESSION_ENV_FILE = "/etc/renovate-agent/session.env"
# The read-only token in SESSION_ENV_FILE that `session_env` hands the session as GH_TOKEN.
SESSION_TOKEN_KEY = "RENOVATE_SESSION_GH_TOKEN"


def session_env(env: Mapping[str, str]) -> dict[str, str]:
    """The unit's environment with `GH_TOKEN` swapped for the session's read-only token.

    A missing read token raises, so a session never starts holding the runner's token.
    """
    if not env.get(SESSION_TOKEN_KEY):
        raise RuntimeError(f"{SESSION_TOKEN_KEY} is unset; {SESSION_ENV_FILE} lacks it")
    out = {k: v for k, v in env.items() if k != SESSION_TOKEN_KEY}
    out["GH_TOKEN"] = env[SESSION_TOKEN_KEY]
    return out


def session_argv(cfg: dict[str, str], requests_dir: str) -> list[str]:
    """The headless Claude command line for this run's prompt.

    `requests_dir` is outside the worktree, so it is added as a working directory: a headless
    session has no one to approve a Write there, and its request files would never land.
    """
    prompt = read_file(cfg["PROMPT_FILE"])
    if not prompt.strip():
        raise RuntimeError(f"prompt file {cfg['PROMPT_FILE']} is empty or missing")
    return [
        cfg.get("CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        "--permission-mode",
        cfg.get("PERMISSION_MODE", "auto"),
        "--model",
        cfg.get("MODEL", "opus"),
        "--output-format",
        "json",
        "--max-budget-usd",
        cfg.get("BUDGET_USD", "25"),
        "--add-dir",
        requests_dir,
    ]


def main(tools: AgentTools = TOOLS, config_path: str = CONFIG) -> int:
    """Run one session, or record why the worktree blocked it; return claude's exit status.

    stdin is /dev/null: without it Claude Code waits 3s for piped input on every start, and
    under systemd there is no stdin to wait for.
    """
    cfg = parse_env_file(config_path)
    state_dir = cfg.get("STATE_DIR", "/var/lib/renovate-agent")
    repo_dir = cfg["REPO_DIR"]
    branch = cfg.get("BRANCH", "worktree-renovate-auto")
    path = os.path.join(
        repo_dir, ".claude", "worktrees", cfg.get("WORKTREE", "renovate-auto")
    )
    reusable, why = worktree_is_reusable(repo_dir, path, branch, tools, cfg["REPO"])
    if not reusable:
        atomic_write(os.path.join(state_dir, BLOCKED_FILE), why)
        return 0
    prepare_worktree(repo_dir, path, branch, tools)
    argv = session_argv(cfg, os.path.join(state_dir, REQUESTS_DIR))
    log("starting session")
    p = subprocess.run(
        argv,
        cwd=path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=session_env(os.environ),
    )
    atomic_write(os.path.join(state_dir, OUTPUT_FILE), p.stdout or "")
    return p.returncode


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    install_excepthook()
    sys.exit(main())
