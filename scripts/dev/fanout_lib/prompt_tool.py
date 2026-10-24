"""The flags that let a headless batch's permission prompts reach claude-guard's allow rules.

A `claude -p` session opens no permission dialog, so claude-guard's PermissionRequest hook
never runs there, and every ask ends as "you haven't granted it yet": an `rm` or `git restore`
inside the batch's own worktree that an interactive session is allowed. `claude-guard
prompt-server` answers the prompt with that same allow path instead and refuses everything
else (dotfiles #998). Measured 2026-10-24: the tool is consulted for a settings ask rule and a
hook's ask, never after a hook's deny, so the deploy-plane hook's asks stay refused here.

A session whose prompt-tool server cannot start exits before its first turn (measured the same
day, rc 1), so a launch checks the shim first (`shim_check`) and refuses, naming it.
"""

import json
import shlex
from pathlib import Path

PROMPT_TOOL = "mcp__claude-guard__decide"


def shim(home: str | None = None) -> str:
    """The claude-guard shim chezmoi installs under the batch user's home."""
    return f"{home or Path.home()}/.local/bin/claude-guard"


def shim_guarded(command: str, home: str | None = None) -> str:
    """`command` run only when the shim is executable; otherwise false, naming it on stderr.

    An `if`, not `test -x … ||`: a reader of the unit's command line splits it at its first
    `||`, which `_step` places after the whole step.
    """
    path = shim(home)
    return f'if [ -x {path} ]; then {command} ; else echo "no claude-guard shim at {path}" >&2; false; fi'


def prompt_tool_args(home: str | None = None) -> list[str]:
    """`--mcp-config` naming the prompt server and `--permission-prompt-tool` naming its tool.

    Not `--strict-mcp-config`: the batch still loads its other servers (`homelab-ui`).
    """
    server = {"command": shim(home), "args": ["prompt-server"]}
    config = json.dumps({"mcpServers": {"claude-guard": server}}, separators=(",", ":"))
    return ["--mcp-config", config, "--permission-prompt-tool", PROMPT_TOOL]


def prompt_tool_flags(home: str | None = None) -> str:
    """`prompt_tool_args`, quoted for the unit's shell command line."""
    return " ".join(shlex.quote(a) for a in prompt_tool_args(home))
