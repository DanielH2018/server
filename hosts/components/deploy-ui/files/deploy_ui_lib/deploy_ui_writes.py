"""Guards and writes for the deploy UI. A guard returns None to allow, else the refusal text.

A landing refuses itself under `DEPLOY_HOLD`, so no guard here reads the hold.
"""

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from deploy_ui_lib.deploy_ui_reads import Run

REQUIRED_HEADER = "X-Deploy-UI"


def write_allowed(headers: Mapping[str, str]) -> str | None:
    """Refuse a write request missing the CSRF-style header or a JSON body."""
    h = {k.lower(): v for k, v in headers.items()}
    if h.get(REQUIRED_HEADER.lower()) != "1":
        return f"missing {REQUIRED_HEADER}: 1 header"
    if not h.get("content-type", "").startswith("application/json"):
        return "body must be application/json"
    return None


def guard_land(pr: str, inflight: list[Run]) -> str | None:
    """Refuse a duplicate landing for the same PR."""
    if any(l.pr == pr for l in inflight):
        return f"a landing for PR {pr} is already running"
    return None


def guard_cancel(pid: int, listed: set[int]) -> str | None:
    """Refuse to cancel a pid the in-flight panel didn't list."""
    if pid not in listed:
        return f"pid {pid} is not a listed landing"
    return None


def spawn_logged(argv: list[str], cwd: Path, log_dir: Path, action: str) -> Path:
    """Start argv detached with stdout+stderr in `<log_dir>/<action>-<ts>-<rand>.log`.

    The pid sits beside it in `.pid` so a later request can address the process.

    The name carries a random suffix because the timestamp alone is one-second granular:
    two writes in the same second named the same file, and the second `open("wb")`
    truncated a log the first process was still writing to and overwrote its `.pid`
    (GitHub issue #1597). `mkstemp` opens O_EXCL, so uniqueness is decided by the
    filesystem rather than by a counter this daemon would have to hold a lock over — and it
    still holds across a daemon restart.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"{action}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-",
        suffix=".log",
        dir=log_dir,
    )
    os.close(fd)
    log = Path(name)
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
