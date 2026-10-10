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
Those lock descriptors and stdio are all the child keeps: it closes every other descriptor its
caller passed down (`close_inherited`), as a detached landing does (issue #3162).

THE CHILD IS A GRANDCHILD, OUTSIDE THE CALLER'S TREE AND CGROUP (issues #3159, #3160).
`lib/detach_fork.py` forks it twice, so a harness that kills the caller's descendant tree
cannot reach it, even in the window before this parent exits. The child then moves itself out
of a fan-out unit's cgroup into `deploy-<pid>.scope`, so stopping the batch's unit does not
kill a playbook mid-apply. The intermediate child's copies of the lock descriptors close when
it exits, which releases nothing: the grandchild still holds the same open file descriptions.

THE CHILD RECORDS THE NOTIFIER'S VERDICT AS ITS EXIT CODE, SO `cc-wait deploy` CAN END ON IT
(issue #3934). The parent writes `<log stem>.pid` and the child writes `<log stem>.rc` through
`lib/detach_fork.py`'s record helpers: 0 when the notifier's health gate settled, non-zero
otherwise. `deploy_probe.py` reads them. The code is written after the service locks are
released, so a caller that chains a second deploy of the same service on the wait's end does
not queue behind this one.
"""

import contextlib
import fcntl
import os
import pwd
import re
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package directories: an importer that did not bootstrap them itself (a
# test, a REPL) finds only this module's own directory, and `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_tools import deploy_under_locks as locked
from deploy_tools.deploy_owed_k8s import discharge_owed_k8s
from deploy_tools.deploy_playbook import annotate, run_playbook
from lib.detach_fork import (
    close_inherited,
    fork_detached,
    leave_unit_cgroup,
    pid_path,
    record_code,
)
from lib.exit_codes import (
    DEPLOY_LOCK_BUSY,
    DEPLOY_LOCK_UNAVAILABLE,
)

# One directory per user, suffixed with the user's name, as land_lib/detach.py's fallback is: the
# first deploy creates it under its own umask, and the operator's 0007 made a shared one 0770,
# which shut the `claude` agent user out of every `--detach` run (#4108).
LOG_DIR = Path(f"/tmp/homelab-deploy-logs-{pwd.getpwuid(os.geteuid()).pw_name}")


def log_path(run: locked.Run, log_dir: Path = LOG_DIR) -> Path:
    """Where the child writes, named after the tags, a UTC stamp and the parent's pid."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", run.label)
    return log_dir / f"deploy-{label}-{stamp}-{os.getpid()}.log"


def wait_command(tags: list[str], log: Path) -> str:
    """The `cc-wait` command that waits on this run, and resumes the wait when re-run."""
    return f"cc-wait deploy {','.join(tags) or 'full'} --log {log}"


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
            *locked.TREE_LOCK_HOLDERS,
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


def notify(run: locked.Run, status: int, log: Path, notifier: str) -> int:
    """Gate the deployed tags' health and post the verdict; the notifier's exit code.

    Runs as a subprocess of the child. The code is 0 only when the gate settled.

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
    return subprocess.run(
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
    ).returncode


@dataclass(frozen=True)
class ChildSteps:
    """The four steps `deploy_and_gate` runs, so a test replaces a field, not a module."""

    run_playbook: Callable[[locked.Run], int] = run_playbook
    annotate: Callable[[locked.Run], None] = annotate
    discharge: Callable[[Path], None] = discharge_owed_k8s
    notify: Callable[[locked.Run, int, Path, str], int] = notify


def deploy_and_gate(
    run: locked.Run, log: Path, notifier: str, steps: ChildSteps | None = None
) -> int:
    """Run the playbook, annotate and discharge a success, then run the notifier's health gate.

    The discharge is the foreground run's `discharge_owed_k8s` (#4087), run before the gate so
    the ledger no longer names the change when the verdict posts.

    Returns:
      The notifier's exit code: 0 when the gate settled, which needs the playbook's 0 too.

    The service locks stay held until the caller's `run.close()`, AFTER the notifier returns
    (#3817). The gate reads one `probe.py health` sample per tag. Released before it, a second
    deploy of the same service could take the lock and roll a `Recreate` workload under that
    sample, and the run would post `unhealthy` about a healthy change. The hold grows by the
    gate's few probe calls; a deploy queued behind it waits `SERVICE_LOCK_WAIT_S`, 1800s.
    land.sh cannot hold its deploy's locks across its own gate, so it waits out the later
    deploy instead (`land_rerolls.later_deploys`).
    """
    steps = steps or ChildSteps()
    status = steps.run_playbook(run)
    # Annotated here, where the run finished: the parent returned long before.
    if status == 0:
        steps.annotate(run)
        steps.discharge(run.repo_root)
    # The snapshot outlives the playbook by exactly this call: the notifier's health gate
    # renders the deployed role's manifests from it to enumerate what to check.
    return steps.notify(run, status, log, notifier)


@dataclass(frozen=True)
class DetachTools:
    """The boundaries `run` and `child` cross, so a test replaces a field, not a module."""

    log_dir: Path = LOG_DIR
    take_tree_lock: Callable[[], int] = take_tree_lock_now
    reap_dead_snapshots: Callable[[Path], None] = locked.reap_dead_snapshots
    make_snapshot: Callable[[locked.Run], None] = locked.make_snapshot
    take_service_locks: Callable[[locked.Run, list[str]], None] = (
        locked.take_service_locks
    )
    fork_detached: Callable[[Callable[[], None]], int] = fork_detached
    leave_unit_cgroup: Callable[[str], str | None] = leave_unit_cgroup
    deploy_and_gate: Callable[[locked.Run, Path, str], int] = deploy_and_gate


def child(
    run: locked.Run, log: Path, notifier: str, tools: DetachTools | None = None
) -> None:
    """The backgrounded half. Never returns: its end is `os._exit`, never deploy_run's frames."""
    tools = tools or DetachTools()
    code = 1
    try:
        null = os.open(os.devnull, os.O_RDONLY)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(out, 2)
        # Every other inherited descriptor goes, `null` and `out` with them, but the locks
        # `run_playbook` passes on (issue #3162).
        close_inherited(fd for fd in (*run.service_fds, run.owner_fd) if fd is not None)
        # Before the playbook starts: a child started while the move is pending stays in the
        # unit's cgroup.
        moved = tools.leave_unit_cgroup("deploy")
        if moved:
            print(moved, flush=True)
        code = tools.deploy_and_gate(run, log, notifier)
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
        # After `run.close()` and the flush: a waiter that reads the code finds the locks free
        # and the verdict line already in the log.
        record_code(log, code)
        os._exit(code)


def run(
    repo_root: Path,
    tags: list[str],
    at_sha: str,
    args: list[str],
    notifier: str,
    tools: DetachTools | None = None,
) -> int:
    """Lock, snapshot, fork; the parent's exit status, 0 once the child is running.

    `notifier` is the path of `deploy_detach_notify.py`, relative to `repo_root`.
    """
    tools = tools or DetachTools()
    state = locked.Run(repo_root=repo_root, tags=tags, at_sha=at_sha, args=args)
    log = log_path(state, tools.log_dir)
    tools.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        full_run_tags: list[str] = []
        tree_fd = tools.take_tree_lock()
        try:
            # Under the tree lock, and the only thing this mode needs it for.
            tools.reap_dead_snapshots(repo_root)
            tools.make_snapshot(state)
            if not tags:
                full_run_tags = locked.enumerate_full_run_tags(state)
        finally:
            os.close(tree_fd)
        try:
            tools.take_service_locks(state, full_run_tags)
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
    # Before the fork, so a `cc-wait deploy` chained on this return finds this run's log and
    # never an earlier one for the same tags.
    log.touch()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = tools.fork_detached(lambda: child(state, log, notifier))
    pid_path(log).write_text(f"{pid}\n")
    # The child owns the snapshot and the locks now. Close this process's copies WITHOUT
    # removing the snapshot: `state.close()` here would delete it from under the playbook.
    for fd in state.service_fds:
        os.close(fd)
    if state.owner_fd is not None:
        os.close(state.owner_fd)
    state.service_fds, state.owner_fd, state.snapshot = [], None, None
    print(f"deploy --detach: running in background (pid {pid}).")
    print(f"  log:  {log}")
    print(f"  wait: {wait_command(tags, log)}")
    print(
        "  Posts to the gitops-deploy Discord webhook when it settles, gated on "
        "'probe.py health <svc>' for every deployed tag that supports it."
    )
    sys.stdout.flush()
    return 0
