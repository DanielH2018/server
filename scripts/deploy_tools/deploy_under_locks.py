"""The locked half of a foreground deploy: tree lock, snapshot, service locks, playbook.

`deploy_run.py` calls `run` once every gate has passed. `--detach` still runs through
`deploy_locked.sh` until slice 4 of #2412 (`docs/deploy-sh-python-port.md`) ports it, so the
values the two share are pinned together by `test_deploy_locked_halves_agree.py`.

TWO LOCKS, GUARDING TWO DIFFERENT THINGS (ADR-0017). `/var/lock/server-git-tree.lock` guards
the git tree and nothing else: gitops-deploy.service, the weekly secret-rotate cron and the
docs refresh all rewrite the tree every deploy renders from. This module holds it only long
enough to copy the commit into a detached worktree under /tmp/homelab-deploy-snapshots --
seconds -- and runs the playbook against that snapshot. `/var/lock/server-deploy-<tag>.lock`
guards the CLUSTER, one lock per deploy tag, held across the whole playbook: two deploys of
one service serialize, two of different services run at once. A run that names no tag takes
server-deploy-all.lock exclusively and every scoped run takes it shared.

THE SNAPSHOT IS OF HEAD (or of --at), NOT THE WORKING TREE. An uncommitted edit is not
deployed. Rendered manifests are then a function of a commit, which is what makes the release
record's `tree_dirty` structurally false and what makes it safe to let the tree move while the
playbook runs.

Exit codes are `exit_codes.py`'s: 0 a finished deploy; 75, 76, 77, 78 and 79 mean NOTHING was
deployed; 20 means the playbook RAN and a task failed, so whatever applied before it is live.
"""

import contextlib
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package directories: an importer that did not bootstrap them itself (a
# test, a REPL) finds only this module's own directory, and `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_tools.exit_codes import (
    DEPLOY_LOCK_BUSY,
    DEPLOY_LOCK_PLAN_FAILED,
    DEPLOY_LOCK_UNAVAILABLE,
    DEPLOY_NO_HOSTS,
    DEPLOY_PLAYBOOK_FAILED,
    DEPLOY_SNAPSHOT_FAILED,
)
from deploy_tools.deploy_playbook import annotate, run_playbook
from lib.git import git, git_stdout
from lib.repo_paths import GITOPS_DEPLOY_FILES

# deploy_locks.py ships to the deployer host as a role file, so it sits on no import path.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
import deploy_locks

# Covers gitops-deploy's worst-case hold of 3240s (STAGING_GATE_TIMEOUT_S 600 +
# STAGING_EXPECT_TIMEOUT_S 120 + K8S_DEPLOY_TIMEOUT_S 900 + K8S_ROLLBACK_TIMEOUT_S 1620), not its
# TimeoutStartSec. It was 1500 until 2026-08-23 and was left behind when the unit's timeout
# grew, so a deploy queued behind a pathological gitops run gave up having deployed nothing
# while that run was still legitimately working. Pinned to the same role defaults the deployer
# reads by test_deploy_lock_wait_budget.py. It is also the budget each per-service lock waits.
LOCK_WAIT = 3300
# The snapshot this run is using, told apart from a dead one by an advisory lock inside it.
OWNER_LOCK = ".deploy-owner.lock"
SNAPSHOT_ROOT_DEFAULT = "/tmp/homelab-deploy-snapshots"
# How long `deploy_tags.py list` may take under the tree lock. ADR-0017's "the hold is
# seconds" rests on it staying fast; measured 2026-09-17 on a warm venv: 0.9s.
TAG_LIST_TIMEOUT_DEFAULT = 120
# How many dead snapshots one locked run removes. The reaper runs under the tree lock and
# `git worktree remove` is ~0.3s each, so a root with hundreds of dead directories would
# otherwise turn the hold into minutes. The rest wait for the next locked run.
REAP_MAX_PER_RUN_DEFAULT = 20


def say(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr, flush=True)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def tree_lock_path() -> str:
    """The git-tree lock (ADR-0011); tests redirect it so they never queue a live tick."""
    return os.environ.get("HOMELAB_DEPLOY_TREE_LOCK") or deploy_locks.TREE_LOCK


def snapshot_root() -> Path:
    return Path(os.environ.get("HOMELAB_DEPLOY_SNAPSHOT_ROOT") or SNAPSHOT_ROOT_DEFAULT)


def lock_wait() -> int:
    """LOCK_WAIT, overridable so a test can prove the budget ends in exit 75."""
    return _env_int("HOMELAB_DEPLOY_LOCK_WAIT", LOCK_WAIT)


class Refused(Exception):
    """Nothing was deployed; `code` is the exit status and the message is already printed."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _LockTimeout(Exception):
    pass


def _flock_timed(fd: int, mode: int, wait_s: float) -> None:
    """Block in flock(2) for up to `wait_s`; raises _LockTimeout when the budget ends.

    Blocking rather than polling keeps a queued deploy visible as a `->` waiter in
    /proc/locks, which is how the deploy page tells a waiter from a holder. The handler must
    RAISE: PEP 475 retries an interrupted flock(2) otherwise.
    """

    def on_alarm(_signum, _frame):
        raise _LockTimeout

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, max(wait_s, 0.001))
    try:
        fcntl.flock(fd, mode)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _open_lock(path: str) -> int:
    """Open a lock file for writing, creating it. Raises OSError."""
    return os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)


def say_lock_unavailable(path: str, exc: OSError) -> None:
    """A lock that failed for a reason that is NOT contention (exit 76)."""
    what = "directory" if os.path.isdir(path) else "file"
    say(
        f"deploy: could not take {path} ({exc.strerror or exc}) -- nothing was deployed.",
        "  This is NOT contention: no deploy is holding the lock, the lock itself failed.",
        f"  Check the lock {what} {path} exists and is writable by this user",
        f"  (ls -ld {path}). Retrying changes nothing until it is; nothing ran, so a",
        "  re-run is safe once fixed.",
    )


def lock_holder(path: str) -> str:
    """The tree lock's holder as land_lib/tools.py:lock_holder formats it, or "".

    fuser prints the holding PIDs on stdout; the lowest is the flock parent, whose children
    inherited the descriptor. Best-effort: a missing fuser or ps names nobody.
    """
    try:
        pids = subprocess.run(
            ["fuser", path], capture_output=True, text=True, check=False
        ).stdout.split()
        numeric = sorted(int(p) for p in pids if p.isdigit())
        if not numeric:
            return ""
        detail = subprocess.run(
            ["ps", "-o", "etimes=,args=", "-p", str(numeric[0])],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return ""
    return f"pid {numeric[0]} (etimes, command): {' '.join(detail.split())}"


@dataclass
class Run:
    """One locked deploy's state, released by `close` whichever way the run ends."""

    repo_root: Path
    tags: list[str]
    at_sha: str
    args: list[str]
    snapshot: Path | None = None
    snapshot_sha: str = ""
    owner_fd: int | None = None
    service_fds: list[int] = field(default_factory=list)
    lock_dir: str = ""

    @property
    def label(self) -> str:
        """The tags as one filesystem-safe word, for the snapshot directory's name.

        Joined with `+`, never `,`: the snapshot is the playbook's cwd, and ansible-core reads
        a comma anywhere in the resolved inventory path as an inline host list, so every
        multi-tag deploy matched no host while exiting 0 (issue #1813).
        """
        return re.sub(r"[^A-Za-z0-9_.-]", "_", "+".join(self.tags) or "full")

    def close(self) -> None:
        for fd in self.service_fds:
            os.close(fd)
        self.service_fds = []
        remove_snapshot(self)


# -- the snapshot ---------------------------------------------------------------------------
#
# DECIDED: ownership is this lock, never the pid in the directory name. `--detach` runs its
# playbook in a process whose pid is not the one in the name, and the name's pid is dead for the
# whole life of a detached deploy -- a pid-based reaper deleted the worktree out from under the
# running playbook. A process cannot lie about holding a flock. (ADR-0017)


def remove_snapshot(run: Run) -> None:
    """Remove this run's snapshot. Safe to call twice and safe to call having made none.

    `git worktree remove --force` deregisters the worktree itself, so no prune here: an
    unconditional one would deregister other sessions' worktrees whose directories are gone.
    """
    if run.owner_fd is not None:
        os.close(run.owner_fd)
        run.owner_fd = None
    if run.snapshot is None:
        return
    removed = git(
        "worktree",
        "remove",
        "--force",
        str(run.snapshot),
        cwd=run.repo_root,
        check=False,
    )
    if removed.returncode != 0:
        shutil.rmtree(run.snapshot, ignore_errors=True)
    run.snapshot = None


def reap_dead_snapshots(repo_root: Path) -> None:
    """Remove snapshots no deploy is using. CALL ONLY WHILE HOLDING THE TREE LOCK.

    A live one is told apart by its owner lock: `flock -n` on it fails while that deploy is
    alive and succeeds the moment it is not, however it died. The tree lock closes the window
    between `git worktree add` creating a directory and its run taking the owner lock, during
    which the directory looks ownerless. Fails open: a snapshot this run cannot reap costs disk.
    """
    root = snapshot_root()
    if not root.is_dir():
        return
    reaped, cap = (
        0,
        _env_int("HOMELAB_DEPLOY_REAP_MAX_PER_RUN", REAP_MAX_PER_RUN_DEFAULT),
    )
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            fd = _open_lock(str(directory / OWNER_LOCK))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue
        finally:
            os.close(fd)
        if reaped >= cap:
            say(
                f"deploy: reaped {reaped} dead snapshots under {root} and stopped at the",
                "  per-run cap; the rest go on the next locked run (REAP_MAX_PER_RUN).",
            )
            break
        removed = git(
            "worktree", "remove", "--force", str(directory), cwd=repo_root, check=False
        )
        if removed.returncode != 0:
            shutil.rmtree(directory, ignore_errors=True)
        reaped += 1
    # Only after reaping something: a prune deregisters every worktree whose directory is
    # missing, other sessions' included.
    if reaped:
        git("worktree", "prune", cwd=repo_root, check=False)


def make_snapshot(run: Run) -> None:
    """Copy the commit into a detached worktree and take its owner lock. Refuses with 77.

    `--detach` so the snapshot claims no branch, which would make the branch unusable
    everywhere else.
    """
    root = snapshot_root()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    directory = root / f"{run.label}-{stamp}-{os.getpid()}"
    error = ""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        error = str(exc)
    if not error:
        added = git(
            "worktree",
            "add",
            "--detach",
            str(directory),
            run.at_sha or "HEAD",
            cwd=run.repo_root,
            check=False,
        )
        error = added.stderr.strip() if added.returncode != 0 else ""
    if not error:
        run.snapshot = directory
        run.snapshot_sha = (
            git_stdout("rev-parse", "--short", "HEAD", cwd=directory, check=False)
            or "unknown"
        )
        try:
            run.owner_fd = _open_lock(str(directory / OWNER_LOCK))
            fcntl.flock(run.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            error = f"could not take the owner lock {directory / OWNER_LOCK}"
            remove_snapshot(run)
    if error:
        say(
            f"deploy: could not snapshot {run.at_sha or 'HEAD'} into {root} -- nothing was deployed.",
            "  " + error.replace("\n", "\n  "),
            "  The playbook renders from a detached worktree of that commit, so without one",
            "  there is nothing to deploy from; retrying alone will not fix the cause above.",
        )
        raise Refused(DEPLOY_SNAPSHOT_FAILED)


def enumerate_full_run_tags(run: Run) -> list[str]:
    """Every deploy tag a full run must lock, read FROM THE SNAPSHOT under the tree lock.

    A subprocess in the snapshot, not an import: the list must be the snapshot's
    `containers_list` read by the snapshot's own parser (the #851 skew otherwise). Collected in
    the order printed and not sorted: `deploy_locks.plan` sorts, and it is the only thing that
    may. An empty list is a failure -- a full run would deploy everything holding nothing.
    """
    timeout = _env_int("HOMELAB_DEPLOY_TAG_LIST_TIMEOUT", TAG_LIST_TIMEOUT_DEFAULT)
    try:
        listed = subprocess.run(
            ["uv", "run", "python", "scripts/deploy_tools/deploy_tags.py", "list"],
            cwd=run.snapshot,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(run.repo_root / ".venv")},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except subprocess.TimeoutExpired:
        say(
            f"deploy: listing the deploy tags took longer than {timeout}s under the",
            "  tree lock, so the run was abandoned -- nothing was deployed. That hold is meant",
            "  to be seconds (ADR-0017); a cold 'uv sync' is the usual cause. Run",
            "  'uv run python scripts/deploy_tools/deploy_tags.py list' once by hand, then retry.",
        )
        raise Refused(DEPLOY_SNAPSHOT_FAILED) from None
    tags = [line.strip() for line in listed.splitlines() if line.strip()]
    if not tags:
        say(
            "deploy: could not list the deploy tags from the snapshot -- nothing was deployed.",
            "  A run with no --tags locks one lock per declared service, so an unreadable list",
            "  means it would deploy everything holding nothing. Check that",
            "  'uv run python scripts/deploy_tools/deploy_tags.py list' works in this checkout.",
        )
        raise Refused(DEPLOY_SNAPSHOT_FAILED)
    return tags


# -- the tree lock --------------------------------------------------------------------------


@contextlib.contextmanager
def tree_lock() -> Iterator[None]:
    """Hold the git-tree lock for the body, printing the wait when there was one.

    The holder is sampled BEFORE this process opens the lock file: fuser reports every process
    with a descriptor on it, so once this process holds one it names ITSELF. A lock taken at
    once reports nothing -- 0s by construction, whatever the clock says (#1881).
    """
    path = tree_lock_path()
    holder = lock_holder(path)
    try:
        fd = _open_lock(path)
    except OSError as exc:
        say_lock_unavailable(path, exc)
        raise Refused(DEPLOY_LOCK_UNAVAILABLE) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            started = time.monotonic()
            try:
                _flock_timed(fd, fcntl.LOCK_EX, lock_wait())
            except _LockTimeout:
                say(
                    f"deploy: could not take {path} after {lock_wait()}s -- nothing was deployed.",
                    "  A deploy is already running. Likely holders: gitops-deploy.service",
                    "  (systemctl status gitops-deploy.service), the weekly secret-rotate cron,",
                    "  or another Claude session (uv run python scripts/dev/prune_worktrees.py).",
                )
                raise Refused(DEPLOY_LOCK_BUSY) from None
            waited = int(time.monotonic() - started)
            if waited > 0:
                line = f"deploy: lock acquired after {waited}s"
                say(f"{line} (holder was: {holder})" if holder else line)
        except OSError as exc:
            say_lock_unavailable(path, exc)
            raise Refused(DEPLOY_LOCK_UNAVAILABLE) from None
        yield
    finally:
        os.close(fd)


# -- the per-service locks ------------------------------------------------------------------
#
# DECIDED: the lock order is whatever `deploy_locks.plan` returns, taken top to bottom, and
# this module neither names a lock nor sorts a tag. The deployer walks the same `plan()` for its
# own locks, so the two orders are one function rather than two agreeing ones. This run takes
# the tree lock, snapshots, RELEASES the tree lock and only then takes service locks, while the
# GitOps deployer holds the tree lock across its service locks; there is no cycle because this
# run never re-takes the tree lock after releasing it. (ADR-0017)


def take_service_locks(
    run: Run, full_run_tags: list[str], plan=deploy_locks.plan
) -> None:
    """Take every lock `plan` names, in its order. Refuses with 75, 76 or 79.

    `plan` is a parameter so a test can hand over an order no sort would produce. A scoped run
    shares `all`; a run with no tags takes `all` exclusively AND each declared tag's own lock.
    """
    try:
        planned = (
            plan(run.tags) if run.tags else plan(full_run_tags, exclusive_all=True)
        )
    except Exception as exc:
        planned, failure = [], f"raised {exc!r}"
    else:
        failure = "" if planned else "named no locks"
    if failure:
        say(
            f"deploy: deploy_locks.plan {failure}, so this run has no lock list and",
            "  nothing was deployed. The wrapper takes only the locks that helper names --",
            "  never a list of its own. Run 'uv run python",
            f"  {GITOPS_DEPLOY_FILES / 'deploy_locks.py'} plan <tag>' by hand to see why.",
        )
        raise Refused(DEPLOY_LOCK_PLAN_FAILED)
    run.lock_dir = os.path.dirname(planned[0].path)
    for lock in planned:
        try:
            fd = _open_lock(lock.path)
        except OSError:
            say(
                f"deploy: could not open the service lock file {lock.path} -- nothing was deployed.",
                f"  {run.lock_dir} must exist and be writable by this user",
                f"  (ls -ld {run.lock_dir}); retrying changes nothing until it is.",
            )
            raise Refused(DEPLOY_LOCK_UNAVAILABLE) from None
        run.service_fds.append(fd)
        mode = fcntl.LOCK_EX if lock.exclusive else fcntl.LOCK_SH
        started = time.monotonic()
        try:
            _flock_timed(fd, mode, lock_wait())
        except _LockTimeout:
            say(
                f"deploy: a service lock under {run.lock_dir} stayed busy for {lock_wait()}s -- nothing",
                "  was deployed. Another deploy of one of these services is in progress:",
                "  gitops-deploy.service, or another Claude session. Retry.",
            )
            raise Refused(DEPLOY_LOCK_BUSY) from None
        except OSError as exc:
            say_lock_unavailable(run.lock_dir, exc)
            raise Refused(DEPLOY_LOCK_UNAVAILABLE) from None
        # Silent at 0s, for the reason the tree lock's line is. land.py books these seconds
        # into the landing's `lock=` field (land_lib/tools.py:in_flock_wait).
        waited = int(time.monotonic() - started)
        if waited > 0:
            say(f"deploy: service lock {lock.name} acquired after {waited}s")


def run(repo_root: Path, tags: list[str], at_sha: str, args: list[str]) -> int:
    """Deploy under the locks; the wrapper's exit status."""
    state = Run(repo_root=repo_root, tags=tags, at_sha=at_sha, args=args)
    try:
        full_run_tags: list[str] = []
        with tree_lock():
            reap_dead_snapshots(repo_root)
            make_snapshot(state)
            if not tags:
                full_run_tags = enumerate_full_run_tags(state)
        take_service_locks(state, full_run_tags)
        status = run_playbook(state)
    except Refused as refused:
        return refused.code
    finally:
        state.close()
    # After the locks are released and only on success, so a failed deploy leaves no marker
    # saying it happened.
    if status == 0:
        annotate(state)
        return 0
    if status == DEPLOY_NO_HOSTS:
        say(
            "deploy: the playbook matched NO host, so nothing was deployed -- the PLAY RECAP",
            "  names none. ansible exits 0 for this, which is why the wrapper checks the recap.",
            "  Read the [WARNING] lines above: an inventory that failed to parse, or a host",
            "  pattern that matched nothing. Fix that, then retry; no task ran.",
        )
        return DEPLOY_NO_HOSTS
    # ansible's own 2/3/4 collide with the wrapper's tag-miss, broad and stale codes, so its
    # number is reported and 20 returned (issue #840).
    say(
        f"deploy: the playbook ran and failed (ansible-playbook exit {status}) -- changes that",
        "  applied before the failing task ARE live. Read the PLAY RECAP and the failing",
        "  TASK above; this is not a tag, staleness or lock refusal.",
    )
    return DEPLOY_PLAYBOOK_FAILED
