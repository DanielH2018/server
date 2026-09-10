"""Guards and writes for the deploy UI. A guard returns None to allow, else the refusal text.

The refusals mirror the repo CLAUDE.md *When to wait* list: nothing lands or deploys under a
`hold_sha`, and the hold is cleared HERE, by an operator who typed the SHA, never bypassed.
`clear_hold` removes `hold_sha` and `hold_plane` together because clearing the SHA alone
orphans the plane marker (gitops_deploy's CLAUDE.md records the incident).
"""

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from deploy_ui_reads import Landing

REQUIRED_HEADER = "X-Deploy-UI"


def write_allowed(headers: Mapping[str, str]) -> str | None:
    """Refuse a write request missing the CSRF-style header or a JSON body."""
    h = {k.lower(): v for k, v in headers.items()}
    if h.get(REQUIRED_HEADER.lower()) != "1":
        return f"missing {REQUIRED_HEADER}: 1 header"
    if not h.get("content-type", "").startswith("application/json"):
        return "body must be application/json"
    return None


def _hold_refusal(hold_sha: str) -> str | None:
    return f"deployer holds {hold_sha}; clear the hold first" if hold_sha else None


def guard_land(pr: str, inflight: list[Landing], hold_sha: str) -> str | None:
    """Refuse a duplicate landing for the same PR, or any landing while a hold is set."""
    if any(l.pr == pr for l in inflight):
        return f"a landing for PR {pr} is already running"
    return _hold_refusal(hold_sha)


def guard_deploy(tag: str, known_tags: set[str], hold_sha: str) -> str | None:
    """Refuse a deploy tag `deploy.sh` doesn't know, or any deploy while a hold is set."""
    if tag not in known_tags:
        return f"{tag} is not a deploy tag (deploy.sh --list-services)"
    return _hold_refusal(hold_sha)


def guard_cancel(pid: int, listed: set[int]) -> str | None:
    """Refuse to cancel a pid the in-flight panel didn't list."""
    if pid not in listed:
        return f"pid {pid} is not a listed landing"
    return None


def clear_hold(state_dir: Path, expected_sha: str) -> str | None:
    """Remove `hold_sha` and `hold_plane` together, only when `expected_sha` matches the live hold."""
    sha_file = state_dir / "hold_sha"
    live = sha_file.read_text().strip() if sha_file.exists() else ""
    if live != expected_sha:
        return f"hold is {live or 'clear'}, not {expected_sha}; reload and retry"
    for name in ("hold_sha", "hold_plane"):
        (state_dir / name).unlink(missing_ok=True)
    return None


def set_override(state_dir: Path, action: str) -> str | None:
    """Set or clear the `staging_gate_override` marker; refuse any other action."""
    p = state_dir / "staging_gate_override"
    if action == "set":
        p.touch()
    elif action == "clear":
        p.unlink(missing_ok=True)
    else:
        return f"action must be set or clear, not {action!r}"
    return None


def spawn_logged(argv: list[str], cwd: Path, log_dir: Path, action: str) -> Path:
    """Start argv detached with stdout+stderr in `<log_dir>/<action>-<ts>.log`.

    The pid sits beside it in `.pid` so a later request can address the process.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{action}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    log.with_suffix(".pid").write_text(str(proc.pid))
    # Reap in a background thread: proc goes out of scope on return, and an un-waited
    # detached child leaves a zombie plus a "still running" ResourceWarning at GC time.
    threading.Thread(target=proc.wait, daemon=True).start()
    return log


def terminate(pid: int) -> None:
    """SIGTERM the children first, then the pid.

    Not killpg: a landing started from a shell shares that shell's group, and killing the
    group takes the operator's session.
    """
    kids = subprocess.run(
        ["pgrep", "-P", str(pid)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    ).stdout.split()
    for k in kids + [str(pid)]:
        try:
            os.kill(int(k), signal.SIGTERM)
        except ProcessLookupError:
            pass


def audit(line: str) -> None:
    """Write one logfmt line to syslog; Alloy ships it to Loki beside the Landings board."""
    subprocess.run(["logger", "-t", "deploy-ui", line], check=False, timeout=10)
