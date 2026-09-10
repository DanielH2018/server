"""The transport seam: every process boundary the dispatcher crosses, as one injectable object.

The land_lib/tools.py shape: a test replaces one field and never patches a module attribute
(the monkeypatch ratchet caps new first-party targets at zero).
"""

import json
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib import signing
from fanout_lib.brief import Issue
from fanout_lib.placement import READ_COMMAND, HostReading, parse_reading

HOSTS = ("daniel-box", "daniel-server")
REPO = "/home/ubuntu/server"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
READ_TIMEOUT_S = 20.0
GH_TIMEOUT_S = 30.0
# The one read every host gets: READ_COMMAND's four memory lines and live-agent count, plus
# the signing-key line the launch gate needs, folded into the same connection because the ssh
# budget (2 reads plus MAX_BATCHES_PER_REMOTE_HOST launches = the 5 `ufw limit ssh` allows
# per 30s) has none spare. The key goes last; parse_reading documents the full line order.
HOST_READ_COMMAND = f"{READ_COMMAND}; {signing.signing_key_read_command(REPO)}"
# `labels` is what the launch gate reads; dropping it from this list would refuse every
# issue rather than fail loudly, which is why issue_from_view indexes it.
ISSUE_FIELDS = "number,title,body,labels"


def _local_host() -> str:
    return socket.gethostname()


def _argv(host: str, command: str, local_host: str) -> list[str]:
    """The argv to run `command` on `host`: local `bash -c` when `host` is `local_host`, else ssh.

    Args:
        host: the target host.
        command: the shell command to run.
        local_host: the name `host` is compared against to decide local vs. remote.

    Returns:
        `["bash", "-c", command]` when `host == local_host`, else
        `["ssh", *SSH_OPTS, host, command]`.
    """
    if host == local_host:
        return ["bash", "-c", command]
    return ["ssh", *SSH_OPTS, host, command]


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
    argv = _argv(host, command, local_host or _local_host())
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )


def issue_from_view(data: dict) -> Issue:
    """Map one `gh issue view --json ISSUE_FIELDS` object onto an `Issue`.

    Args:
        data: the decoded JSON object for one issue.

    Returns:
        The issue, carrying its label names.

    Raises:
        KeyError: a field ISSUE_FIELDS asks for is missing. `labels` is read with `[]`
            rather than `.get()` on purpose: gh returns an empty list for an unlabelled
            issue, so an absent key means the fetch stopped asking for it — and the launch
            gate would then refuse every issue instead of the unlabelled ones.
    """
    return Issue(
        data["number"],
        data["title"],
        data["body"],
        tuple(str(label["name"]) for label in data["labels"]),
    )


def gh_issue(number: int) -> Issue:
    """Fetch one GitHub issue by number via `gh issue view`.

    Raises:
        subprocess.CalledProcessError: `gh` exited non-zero.
        subprocess.TimeoutExpired: `gh` did not answer within GH_TIMEOUT_S. Unbounded, one
            hung fetch mid-loop would strand every batch already launched.
    """
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", ISSUE_FIELDS],
        capture_output=True,
        text=True,
        check=True,
        timeout=GH_TIMEOUT_S,
    ).stdout
    return issue_from_view(json.loads(out))


@dataclass(frozen=True)
class Tools:
    """The dispatcher's boundaries: run a command, fetch an issue, read the signing keys."""

    run: Callable[..., subprocess.CompletedProcess] = run_command
    gh_issue: Callable[[int], Issue] = gh_issue
    signing_keys: Callable[[], frozenset[str]] = signing.registered_signing_keys


def read_host(tools: Tools, host: str) -> HostReading | str:
    """Take the headroom reading for `host`, or return why it could not be taken.

    Never guesses: an unreachable host, a timeout, or an unparseable reply all come back
    as a one-line string rather than a fabricated `HostReading`.
    """
    try:
        proc = tools.run(host, HOST_READ_COMMAND, READ_TIMEOUT_S, None)
    except subprocess.TimeoutExpired:
        return "%s: headroom read timed out" % host
    # The signing-key read runs last, so the exit status is its `cat`'s: 0 for a key file, 1
    # for a missing one, which the parse refuses on the empty line rather than here.
    if proc.returncode not in (0, 1):
        return "%s: headroom read failed (%d): %s" % (
            host,
            proc.returncode,
            proc.stderr.strip(),
        )
    try:
        return parse_reading(host, proc.stdout)
    except ValueError as exc:
        return str(exc)


def error_text(exc: BaseException) -> str:
    """The readable text of a subprocess failure, from either exception shape."""
    # CalledProcessError.stderr is text under `text=True`; TimeoutExpired.stderr is bytes
    # or None. Printing either straight would raise inside the handler that exists to make
    # the failure clean.
    err = getattr(exc, "stderr", None)
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return (err or str(exc)).strip()


def registered_keys(tools: Tools) -> frozenset[str] | str:
    """The account's registered signing keys, or a one-line reason they could not be read."""
    try:
        return tools.signing_keys()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        return (
            "could not read the GitHub account's registered signing keys "
            f"({error_text(exc)})"
        )


def verified_hosts(
    readings: list[HostReading], registered: frozenset[str]
) -> tuple[list[HostReading], list[str]]:
    """Split readings into the hosts GitHub verifies and the reasons the rest were dropped.

    A dropped host is not a failure to retry: its commits would read `verified=false
    reason=unknown_key`, and the PR the agent opens there cannot merge past the
    verified-signatures rule until someone re-signs the branch by hand (#1615).

    Args:
        readings: the candidate hosts' readings.
        registered: the account's registered signing keys.

    Returns:
        The readings to place on, and one reason per host dropped — never the key itself.
    """
    keep: list[HostReading] = []
    reasons: list[str] = []
    for r in readings:
        reason = signing.unverified_reason(r.host, r.signing_key, registered)
        if reason is None:
            keep.append(r)
        else:
            reasons.append(reason)
    return keep, reasons
