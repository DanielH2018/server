"""The transport seam: every process boundary the dispatcher crosses, as one injectable object.

The land_lib/tools.py shape: a test replaces one field and never patches a module attribute
(the monkeypatch ratchet caps new first-party targets at zero).
"""

import json
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.brief import Issue
from fanout_lib.placement import READ_COMMAND, HostReading, parse_reading

HOSTS = ("daniel-box", "daniel-server")
REPO = "/home/ubuntu/server"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
READ_TIMEOUT_S = 20.0


def _local_host() -> str:
    return socket.gethostname()


def run_command(
    host: str,
    command: str,
    timeout: float,
    stdin: str | None = None,
    local_host: str | None = None,
) -> subprocess.CompletedProcess:
    """Run `command` on `host` — locally when it is this host, else over ssh.

    Args:
        host: the target host.
        command: the shell command to run.
        timeout: seconds to wait before raising `subprocess.TimeoutExpired`.
        stdin: text piped to the command's stdin, or None.
        local_host: this host's own name; defaults to `socket.gethostname()`.

    Returns:
        The finished `subprocess.CompletedProcess` (never raises on a non-zero exit).
    """
    me = local_host or _local_host()
    argv = ["bash", "-c", command] if host == me else ["ssh", *SSH_OPTS, host, command]
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )


def gh_issue(number: int) -> Issue:
    """Fetch one GitHub issue by number via `gh issue view`."""
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "number,title,body"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(out)
    return Issue(data["number"], data["title"], data["body"])


@dataclass(frozen=True)
class Tools:
    run: Callable[..., subprocess.CompletedProcess] = run_command
    gh_issue: Callable[[int], Issue] = gh_issue
    local_host: str = field(default_factory=_local_host)


def read_host(tools: Tools, host: str) -> HostReading | str:
    """Take the headroom reading for `host`, or return why it could not be taken.

    Never guesses: an unreachable host, a timeout, or an unparseable reply all come back
    as a one-line string rather than a fabricated `HostReading`.
    """
    try:
        proc = tools.run(host, READ_COMMAND, READ_TIMEOUT_S, None)
    except subprocess.TimeoutExpired:
        return "%s: headroom read timed out" % host
    if proc.returncode not in (0, 1):  # 1 is pgrep's zero-count exit
        return "%s: headroom read failed (%d): %s" % (
            host,
            proc.returncode,
            proc.stderr.strip(),
        )
    try:
        return parse_reading(host, proc.stdout)
    except ValueError as exc:
        return str(exc)
