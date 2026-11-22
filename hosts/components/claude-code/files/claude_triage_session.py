#!/usr/bin/env python3
"""Run the headless triage session the controller asked for, and leave its result for it.

Usage: claude_triage_session.py   (takes no arguments; -h/--help prints this text)

`homelab-claude-triage-session.service` runs this; the triage runner, `claude_triage_run.py`,
starts that unit and waits for it. The runner writes the framed prompt to
`<EXCHANGE_DIR>/in/prompt.txt`. This script feeds it to `claude -p` on stdin, so the episode
text is on no command line, and writes the session's JSON result to
`<EXCHANGE_DIR>/out/result.json`. It prints nothing the session said: a unit's output goes to
the journal, and a line the session shaped there could raise the next episode. The unit's
`TimeoutStartSec` bounds the session, and its `KillSignal=SIGINT` ends the turn
cleanly.

The session's home is a tmpfs that ends with the unit, so once claude exits, on its own or at the
wall clock, this script copies the transcript from `<SESSION_HOME>` to
`<EXCHANGE_DIR>/out/transcript.jsonl`, where the runner keeps it beside the run record.
"""

import json
import os
import signal
import subprocess
import sys

# The script runs from /opt/claude-triage, where component.yml's `lib: host_lib.py` entry
# ships host_lib.py beside it.
from claude_config_lock import hold_settings
from host_lib import atomic_write, export_transcript, parse_env_file

CONFIG = "/etc/claude-triage/config.env"
IN_DIR = "in"
OUT_DIR = "out"
PROMPT_NAME = "prompt.txt"
RESULT_NAME = "result.json"

# The structure the session must return. Kept small: every field becomes a report section.
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "finding": {"type": "string"},
                },
                "required": ["source", "finding"],
            },
        },
        "proposed_fix": {"type": "string"},
        "follow_up": {"type": "string"},
        # A key of the restart allowlist. The runner checks it and starts the broker; the
        # session has no start right of its own.
        "restart": {"type": "string"},
    },
    "required": ["summary", "root_cause", "confidence", "evidence", "proposed_fix"],
}

# Diagnose-only. The agent user cannot reach root or write the cluster anyway; these keep a
# diagnosis from editing the clone or starting a landing it was not asked for.
DENIED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash(systemctl start homelab-claude-land@*)",
    "Bash(gh pr create*)",
    "Bash(git push*)",
)

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# The unit's LoadCredential= name for the token file.
TOKEN_CREDENTIAL = "claude_oauth_token"


def build_argv(cfg: dict[str, str], settings: str) -> list[str]:
    """The session's argv. The prompt is not in it: `claude -p` reads it from stdin.

    `settings` is the root-owned file homelab-claude-config-render published from the verified
    dotfiles tag: the shared guard hooks and the deny floor, which the session user cannot edit.
    """
    return [
        cfg.get("CLAUDE_BIN", "claude"),
        "-p",
        "--settings",
        settings,
        "--model",
        cfg.get("MODEL", "opus"),
        "--permission-mode",
        "auto",
        # Nobody answers a prompt here, so anything that would ask is denied at once rather
        # than holding the run until its wall clock.
        "--permission-prompts",
        "none",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA, separators=(",", ":")),
        "--max-budget-usd",
        cfg.get("BUDGET_USD", "10"),
        "--max-turns",
        cfg.get("MAX_TURNS", "60"),
        "--disallowedTools",
        *DENIED_TOOLS,
        # Session persistence stays on: it writes the transcript export_transcript copies out.
    ]


def session_env(environ: dict[str, str]) -> dict[str, str]:
    """`environ` with the session's Claude token set from the unit's credential, whitespace removed.

    The credential file holds the token as it was pasted, and `claude setup-token` wraps it onto
    an indented second line: a paste that keeps the wrap makes Claude answer "Not logged in". A
    token holds no whitespace, so joining the whole file loses nothing and rejoins
    a wrapped token. Without the credential, a token already in `environ` is joined the same way.
    """
    env = dict(environ)
    token = env.get(TOKEN_ENV)
    credentials = env.get("CREDENTIALS_DIRECTORY")
    if credentials:
        path = os.path.join(credentials, TOKEN_CREDENTIAL)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                token = f.read()
    if token is not None:
        env[TOKEN_ENV] = "".join(token.split())
    return env


def main(argv: list[str], config_path: str = CONFIG) -> int:
    """Run the session over the prompt the controller left; 0 once the result is written."""
    if argv[1:] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    cfg = parse_env_file(config_path)
    settings = hold_settings(cfg["SETTINGS_FILE"])
    exchange = cfg["EXCHANGE_DIR"]
    with open(os.path.join(exchange, IN_DIR, PROMPT_NAME), encoding="utf-8") as f:
        prompt = f.read()
    # systemd sends the wall clock's SIGINT to the whole unit. A handler, not SIG_IGN, which
    # claude would inherit: claude still stops, and this script lives on to copy the transcript.
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    p = subprocess.run(
        build_argv(cfg, settings),
        cwd=cfg.get("WORKDIR", os.path.expanduser("~/server")),
        env=session_env(dict(os.environ)),
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )
    out = os.path.join(exchange, OUT_DIR)
    atomic_write(os.path.join(out, RESULT_NAME), p.stdout or "")
    if home := cfg.get("SESSION_HOME"):
        export_transcript(home, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
