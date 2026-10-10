"""Drop the `k8s_unapplied` lines a successful `deploy.sh` carried, as the next tick would (#4087).

A landing whose tick merged the range first records the change, then deploys it, and the
deployer discharges the line only at the start of its next tick. Until then the SessionStart
banner and `probe.py gitops-state` report a deployed change as owed. A hand deploy is invisible
to the tick in the same way. `deploy_under_locks.run` and `deploy_detach.deploy_and_gate` call
`discharge_owed_k8s` after a successful playbook, and it runs
`deploy_k8s_owed.discharge_k8s_unapplied`, the deployer's own rule, so a deploy drops exactly
the lines a tick would.
"""

import fcntl
import functools
import os
import sys
import time
from pathlib import Path

# Reach the sibling package directories: an importer that did not bootstrap them itself (a
# test, a REPL) finds only this module's own directory, and `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES

# The deployer's modules ship to the host as role files, so they sit on no import path.
# `deploy_state` reaches `host_lib` for its atomic write, as in `gitops_state.py`.
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
sys.path.insert(0, str(HOST_LIB_FILES))
import deploy_locks

# How long the discharge waits for the tree lock after a deploy. A tick holds it for minutes,
# and that tick's own reconcile discharges the same lines, so this does not wait it out.
OWED_LOCK_WAIT_S = 5.0
LOCK_POLL_S = 0.1
# Redirect the deployer's state directory and the k8s release records, so a test neither
# rewrites the host's ledger nor reads its records. The tree lock reads the variable
# `deploy_under_locks.tree_lock_path` reads.
GITOPS_STATE_DIR_ENV = "HOMELAB_DEPLOY_GITOPS_STATE_DIR"
RELEASE_DIR_ENV = "HOMELAB_DEPLOY_RELEASE_DIR"
TREE_LOCK_ENV = "HOMELAB_DEPLOY_TREE_LOCK"


def say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _take_lock(fd: int, wait_s: float) -> bool:
    """Poll for the tree lock for up to `wait_s`; whether it was taken."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_S)


def discharge_owed_k8s(
    repo_root: Path, tools=None, lock_wait_s: float = OWED_LOCK_WAIT_S
) -> None:
    """Drop each `k8s_unapplied` line a release record now carries.

    Best effort, after the deploy succeeded: any failure is one line on stderr and nothing is
    raised, because the next tick discharges the same lines. The tree lock serialises the
    ledger rewrite against a tick, and a tick holding it longer than `lock_wait_s` skips this.

    Args:
      repo_root: the checkout this deploy ran from, where the ancestry is asked.
      tools: the deployer's `DeployTools`. None builds the production one, which reads the
        host's release records, or the directory `RELEASE_DIR_ENV` names.
      lock_wait_s: how long to wait for the tree lock.
    """
    try:
        import deploy_k8s_owed
        import deploy_release
        from deploy_config import Config
        from deploy_state import STATE_DIR, DeployerState
        from deploy_toolbox import DeployTools

        if tools is None:
            release_dir = (
                os.environ.get(RELEASE_DIR_ENV) or deploy_release.K8S_RELEASE_DIR
            )
            tools = DeployTools(
                release_commit=functools.partial(
                    deploy_release.release_commit, release_dir=release_dir
                )
            )
        lock = os.environ.get(TREE_LOCK_ENV) or deploy_locks.TREE_LOCK
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o666)
        try:
            if not _take_lock(fd, lock_wait_s):
                say(
                    f"deploy: a gitops tick held the tree lock past {lock_wait_s}s, so the "
                    "k8s_unapplied ledger was not checked; the next tick discharges what "
                    "this deploy carried"
                )
                return
            deploy_k8s_owed.discharge_k8s_unapplied(
                tools,
                DeployerState(os.environ.get(GITOPS_STATE_DIR_ENV) or STATE_DIR),
                Config(repo=str(repo_root)),
            )
        finally:
            os.close(fd)
    # Broad on purpose: the deploy already succeeded, and nothing here may change that.
    except Exception as exc:
        say(
            f"deploy: did not check the k8s_unapplied ledger ({type(exc).__name__}: {exc}); "
            "the next gitops tick discharges what this deploy carried"
        )
