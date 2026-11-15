#!/usr/bin/env python3
"""The Renovate agent's session, run in its own unit that the runner starts.

`homelab-renovate-agent-session.service` runs this as the session user (guardrails plan B2r).
It runs the headless Claude session in the controller's clone, which the session reads and cannot
write, and writes what the session printed to `OUTPUT_FILE` in the exchange's `out/` directory.
The runner, which runs as the controller, reads that file after the unit ends, as untrusted data.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_toolbox import log, read_file
from host_lib import atomic_write, install_excepthook, parse_env_file
from run_requests import REQUESTS_DIR

# Holds no secret; the runner's config.env, with the webhook, is the controller's alone.
CONFIG = "/usr/lib/homelab/renovate-agent/session.conf"
OUT_DIR = "out"
OUTPUT_FILE = "session.out"
# The unit's EnvironmentFile: the session's read-only GitHub token and Claude credential, root 0600.
SESSION_ENV_FILE = "/etc/renovate-agent/session.env"


def session_argv(cfg: dict[str, str], requests_dir: str) -> list[str]:
    """The headless Claude command line for this run's prompt.

    `requests_dir` is outside the clone, so it is added as a working directory: a headless
    session has no one to approve a Write there, and its request files would never land.
    `SETTINGS_FILE` is the packaged, root-owned settings file: the guard hooks and the deny floor,
    which the session cannot edit.
    """
    prompt = read_file(cfg["PROMPT_FILE"])
    if not prompt.strip():
        raise RuntimeError(f"prompt file {cfg['PROMPT_FILE']} is empty or missing")
    return [
        cfg.get("CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        *(["--settings", cfg["SETTINGS_FILE"]] if cfg.get("SETTINGS_FILE") else []),
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


def main(config_path: str = CONFIG) -> int:
    """Run one session in the clone and leave its output for the runner; return claude's status.

    stdin is /dev/null: without it Claude Code waits 3s for piped input on every start, and
    under systemd there is no stdin to wait for.
    """
    cfg = parse_env_file(config_path)
    exchange = cfg["EXCHANGE_DIR"]
    out = os.path.join(exchange, OUT_DIR)
    argv = session_argv(cfg, os.path.join(exchange, REQUESTS_DIR))
    log("starting session")
    p = subprocess.run(
        argv,
        cwd=cfg["REPO_DIR"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    atomic_write(os.path.join(out, OUTPUT_FILE), p.stdout or "")
    return p.returncode


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    install_excepthook()
    sys.exit(main())
