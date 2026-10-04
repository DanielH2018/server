"""Guards and writes for the deploy UI. A guard returns None to allow, else the refusal text.

The refusals mirror the repo CLAUDE.md *When to wait* list: nothing lands or deploys under a
`hold_sha`, and the hold is cleared HERE, by an operator who typed the SHA, never bypassed.
`clear_hold` removes `hold_sha` and the `owed` ledger's `hold_plane` lines together because
clearing the SHA alone orphans the planes (gitops_deploy's CLAUDE.md records the incident).
"""

import fcntl
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from deploy_ui_reads import HOLD_PLANE_SEP, Run
from gitops_ledger import OWED_HOLD_PLANE, drop_owed, owed_line_key
from gitops_markers import MARKERS

# Seconds a Clear waits for the git-tree lock before it refuses. `gitops_state.py` waits as
# long for the same reason: an operator is waiting on the reply, a tick can hold the lock for
# most of an hour, and a refused Clear costs nothing to repeat.
LOCK_WAIT_S = 5.0

REQUIRED_HEADER = "X-Deploy-UI"

# `deploy.sh --list-services` prints the block tags and Ansible's reserved `always` alongside
# the service names, because all of them are valid `--tags` values from a terminal. None of
# them is valid HERE: `--tags config` runs the config half of every container role, so
# accepting one turns a single-service button into a fleet-wide deploy (GitHub issue #1596).
# The page only ever posts a service name from the stale panel; this is what stops a
# hand-made POST doing more.
#
# A literal, because the daemon runs under `uv run --no-project` outside the repo venv and
# imports nothing from `scripts/`. It mirrors `BLOCK_TAGS | RESERVED_TAGS` in
# `scripts/deploy_tools/deploy_tags.py`, and the tests — which pytest CAN import both from —
# assert the two sets are equal, so a new block tag there fails here rather than silently
# re-opening the hole.
NON_SERVICE_TAGS = frozenset({"config", "deploy", "cron", "always"})


def service_tags(listed: set[str]) -> set[str]:
    """The deployable service tags among `deploy.sh --list-services` output."""
    return set(listed) - NON_SERVICE_TAGS


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


def guard_land(pr: str, inflight: list[Run], hold_sha: str) -> str | None:
    """Refuse a duplicate landing for the same PR, or any landing while a hold is set."""
    if any(l.pr == pr for l in inflight):
        return f"a landing for PR {pr} is already running"
    return _hold_refusal(hold_sha)


def guard_deploy(tag: str, deployable: set[str], hold_sha: str) -> str | None:
    """Refuse a tag naming no single service, or any deploy while a hold is set.

    `deployable` is `deploy.sh --list-services` with `NON_SERVICE_TAGS` removed, so a block
    tag is refused here while that command still lists it. The message says so rather than
    sending the operator at a command whose output contradicts the refusal.
    """
    if tag not in deployable:
        return (
            f"{tag} is not a deployable service tag. `deploy.sh --list-services` also lists "
            f"the block tags ({', '.join(sorted(NON_SERVICE_TAGS))}); this UI deploys one "
            f"service at a time and refuses those."
        )
    return _hold_refusal(hold_sha)


def guard_cancel(pid: int, listed: set[int]) -> str | None:
    """Refuse to cancel a pid the in-flight panel didn't list."""
    if pid not in listed:
        return f"pid {pid} is not a listed landing"
    return None


def _held_plane_subjects(text: str) -> set[str]:
    """Every subject a `hold_plane` line of the `owed` ledger names, torn lines included."""
    keys = (owed_line_key(line) for line in text.splitlines())
    return {k[1] for k in keys if k is not None and k[0] == OWED_HOLD_PLANE}


def _drop_held_planes(state_dir: Path, tree_lock: Path, wait_s: float) -> str | None:
    """Drop every `hold_plane` line from the `owed` ledger. None on success, else the refusal.

    The ledger holds other classes the deployer rewrites under the git-tree lock, so this
    rewrite takes that lock too: unlocked, a tick's write between the read and the replace
    would be lost. A ledger with no such line is not touched and no lock is taken, which is
    every Clear with no plane held.

    `surrogateescape` carries a torn byte in another class's line through the rewrite
    unchanged, where a lossy decode would rewrite it as U+FFFD.
    """
    owed = state_dir / MARKERS["owed"]
    try:
        if not _held_plane_subjects(owed.read_text(errors="surrogateescape")):
            return None
    except FileNotFoundError:
        return None
    with open(tree_lock, "a") as lock:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return "a tick or deploy holds the git-tree lock; retry once it finishes"
                time.sleep(0.1)
        # Re-read under the lock: the tick may have rewritten the ledger since.
        text = owed.read_text(errors="surrogateescape")
        kept, _ = drop_owed(text, OWED_HOLD_PLANE, _held_plane_subjects(text))
        tmp = owed.with_name(owed.name + ".tmp")
        tmp.write_text(kept, errors="surrogateescape")
        os.replace(tmp, owed)
    return None


def clear_hold(
    state_dir: Path, expected_sha: str, tree_lock: Path, wait_s: float = LOCK_WAIT_S
) -> str | None:
    """Remove `hold_sha` and every plane the hold waits on, only when `expected_sha` matches.

    The planes are the `owed` ledger's `hold_plane` lines, which `gitops_ledger.held_planes`
    reads. They go first, because dropping them is the half that can refuse: a refusal then
    leaves the hold whole. `wait_s` bounds the wait for the
    git-tree lock.
    """
    sha_file = state_dir / MARKERS["hold"]
    live = sha_file.read_text().strip() if sha_file.exists() else ""
    if live != expected_sha:
        return f"hold is {live or 'clear'}, not {expected_sha}; reload and retry"
    refusal = _drop_held_planes(state_dir, tree_lock, wait_s)
    if refusal:
        return refusal
    sha_file.unlink(missing_ok=True)
    return None


def hold_cleared_message(dropped: list[str]) -> str:
    """What the page says after a Clear, naming every plane it just stopped recording.

    Args:
        dropped: the `hold_plane` entries the Clear removed, from `hold_plane_entries`.

    A Clear removes every `hold_plane` line, whatever it holds. Since #2381 that is one entry
    per failed apply, and the deployer clears them one at a time as each plane is applied — so a
    Clear can drop entries for planes nobody has applied, and after it nothing records them
    (#2453). The message names them and says what is owed.
    """
    if not dropped:
        return "hold cleared (hold_sha and hold_plane)"
    return (
        f"hold cleared (hold_sha and hold_plane). {len(dropped)} plane(s) were recorded as "
        f"unapplied and are now recorded nowhere — apply each by hand: "
        + HOLD_PLANE_SEP.join(dropped)
    )


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
