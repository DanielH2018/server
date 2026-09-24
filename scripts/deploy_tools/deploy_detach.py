"""`deploy.sh --detach`: take the locks here, then run the playbook in a forked child.

`deploy_run.py` calls `run` once every gate has passed. The locks, the snapshot and the tag
enumeration are `deploy_under_locks.py`'s, taken synchronously, so a refusal still exits with
its own code. Only the playbook run, the annotation and the Discord notifier move to the child,
which is the ~83% of a deploy spent waiting on rollouts.

The tree lock is taken NON-BLOCKING: waiting up to LOCK_WAIT before returning would defeat the
point of --detach, so contention exits 75 at once. The service locks WAIT, as in a foreground
run: a service lock is held by another deploy of the SAME service, where queueing is the right
behaviour and the wait is bounded by the deploy it is behind.

The child is the process `running in background (pid N)` names. It holds the owner lock and the
service locks through descriptors the fork copied, and the parent closes its own copies, so the
locks follow the child. A flock is released only when every descriptor on its open file
description is closed.
"""

import contextlib
import fcntl
import os
import re
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package directories: an importer that did not bootstrap them itself (a
# test, a REPL) finds only this module's own directory, and `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_tools import deploy_under_locks as locked
from deploy_tools.deploy_playbook import annotate, run_playbook
from deploy_tools.exit_codes import (
    DEPLOY_LOCK_BUSY,
    DEPLOY_LOCK_UNAVAILABLE,
)

LOG_DIR = Path("/tmp/homelab-deploy-logs")


def log_path(run: locked.Run) -> Path:
    """Where the child writes, named after the tags, a UTC stamp and the parent's pid."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", run.label)
    return LOG_DIR / f"deploy-{label}-{stamp}-{os.getpid()}.log"


def take_tree_lock_now() -> int:
    """The tree lock, non-blocking; its descriptor. Refuses with 75 or 76."""
    path = locked.tree_lock_path()
    try:
        fd = locked._open_lock(path)
    except OSError as exc:
        locked.say_lock_unavailable(path, exc)
        raise locked.Refused(DEPLOY_LOCK_UNAVAILABLE) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        locked.say(
            f"deploy --detach: could not take {path} right now -- nothing was deployed.",
            "  A deploy is already running. Likely holders: gitops-deploy.service",
            "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron,",
            "  or another Claude session (uv run python scripts/dev/prune_worktrees.py).",
            f"  --detach fails fast on contention rather than queuing for {locked.LOCK_WAIT}s --",
            "  retry shortly, or drop --detach to queue normally.",
        )
        raise locked.Refused(DEPLOY_LOCK_BUSY) from None
    except OSError as exc:
        os.close(fd)
        locked.say_lock_unavailable(path, exc)
        raise locked.Refused(DEPLOY_LOCK_UNAVAILABLE) from None
    # Always 0s by construction: a non-blocking flock takes the lock at once or refuses. Printed
    # anyway, so the log shape does not depend on the mode and land.py books the same field.
    locked.say("deploy: lock acquired after 0s")
    return fd


def notify(run: locked.Run, status: int, log: Path, notifier: str) -> None:
    """Gate the deployed tags' health and post the verdict, as a subprocess of the child.

    DECIDED: the two halves of this gate come from different trees, and that is accepted.
    `--cwd <snapshot>` makes probe.py render the DEPLOYED commit's manifests, while the notifier
    script itself -- and so its `NOT_APPLICABLE_MARKERS`, the list that turns a probe message into
    `skipped` -- is the CALLING checkout's copy. The marker list is the notifier's own vocabulary
    for reading probe output, not a fact about the deployed tree, so answering it from the tree
    running the notifier is the right source. The failure it admits needs ONE commit to reword a
    probe health message and its marker together, deployed by a checkout that does not yet carry
    the reword; the same split was ruled met on the land path, where land_lib gates from a
    snapshot with its own code from the primary.

    A subprocess, not an import of its `main`: the black-box `--detach` tests stub it by argv,
    and an import would have them post to the host's real webhook and probe production.
    UV_PROJECT_ENVIRONMENT for the reason `run_playbook` sets it.
    """
    subprocess.run(
        [
            "uv",
            "run",
            "python",
            notifier,
            "--status",
            str(status),
            "--log",
            str(log),
            "--cwd",
            str(run.snapshot),
            "--tags",
            ",".join(run.tags),
        ],
        cwd=run.repo_root,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(run.repo_root / ".venv")},
        check=False,
    )


def child(run: locked.Run, log: Path, notifier: str) -> None:
    """The backgrounded half. Never returns: its end is `os._exit`, never deploy_run's frames."""
    code = 1
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDONLY)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        os.close(null)
        os.close(out)
        status = run_playbook(run)
        for fd in run.service_fds:
            os.close(fd)
        run.service_fds = []
        # Annotated here, where the run finished: the parent returned long before.
        if status == 0:
            annotate(run)
        # The snapshot outlives the playbook by exactly this call: the notifier's health gate
        # renders the deployed role's manifests from it to enumerate what to check.
        notify(run, status, log, notifier)
        code = 0
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    except Exception:
        traceback.print_exc()
    finally:
        with contextlib.suppress(Exception):
            run.close()
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(code)


def run(
    repo_root: Path, tags: list[str], at_sha: str, args: list[str], notifier: str
) -> int:
    """Lock, snapshot, fork; the parent's exit status, 0 once the child is running.

    `notifier` is the path of `deploy_detach_notify.py`, relative to `repo_root`.
    """
    state = locked.Run(repo_root=repo_root, tags=tags, at_sha=at_sha, args=args)
    log = log_path(state)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        full_run_tags: list[str] = []
        tree_fd = take_tree_lock_now()
        try:
            # Under the tree lock, and the only thing this mode needs it for.
            locked.reap_dead_snapshots(repo_root)
            locked.make_snapshot(state)
            if not tags:
                full_run_tags = locked.enumerate_full_run_tags(state)
        finally:
            os.close(tree_fd)
        try:
            locked.take_service_locks(state, full_run_tags)
        except locked.Refused as refused:
            if refused.code == DEPLOY_LOCK_BUSY:
                locked.say(
                    "deploy --detach: a deploy of one of these services held its lock for the",
                    f"  full {locked.lock_wait()}s -- nothing was deployed. Retry.",
                )
            raise
    except locked.Refused as refused:
        state.close()
        return refused.code
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        child(state, log, notifier)
    # The child owns the snapshot and the locks now. Close this process's copies WITHOUT
    # removing the snapshot: `state.close()` here would delete it from under the playbook.
    for fd in state.service_fds:
        os.close(fd)
    if state.owner_fd is not None:
        os.close(state.owner_fd)
    state.service_fds, state.owner_fd, state.snapshot = [], None, None
    print(f"deploy --detach: running in background (pid {pid}).")
    print(f"  log:  {log}")
    print(f"  tail: tail -f {log}")
    print(
        "  Posts to the gitops-deploy Discord webhook when it settles, gated on "
        "'probe.py health <svc>' for every deployed tag that supports it."
    )
    sys.stdout.flush()
    return 0
