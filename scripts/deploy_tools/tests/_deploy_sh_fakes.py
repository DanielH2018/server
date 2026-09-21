"""The throwaway repo and environment every `deploy.sh` test runs the wrapper against.

`deploy.sh` copies HEAD into a detached worktree before it runs a playbook (ADR-0017), and
takes one lock per service under /var/lock. Run against this checkout with the real
environment, every test here would register a worktree in the real `.git` and flock files a
live deploy uses. These three helpers are what keep a run inside its own tmp_path.

A module rather than conftest fixtures: `from conftest import x` resolves to whichever
conftest.py sys.path reached first once the whole repo suite runs, and this repo has three.
`_land_fakes.py` is here for the same reason.

Typical usage example:

    bin_dir = ...  # stub uv, flock, ...
    repo = make_snapshot_repo(tmp_path / "repo")
    env = deploy_sh_env(tmp_path, bin_dir)
    subprocess.run([DEPLOY_SH, "--tags", "x"], cwd=repo, env=env)
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
# The module `deploy.sh` reads its service locks from, at the path the wrapper runs it by.
# `make_snapshot_repo` copies it into every throwaway repo, so the wrapper's `uv run python
# ansible/.../deploy_locks.py plan` resolves there the way it does in a real checkout.
DEPLOY_LOCKS_REL = Path("ansible/roles/setup/gitops_deploy/files/deploy_locks.py")

# The shared `flock` stub: drop flock's own options and its lock-file argument, then run
# whatever is left. No real lock is taken, so no test can interleave with a live deploy.
# `-s` and `-x` are in the single-shift arm because a scoped deploy takes
# `server-deploy-all.lock` shared and the snapshot owner lock is taken `-n -x`; without them
# the stub tries to exec `-w`.
FLOCK_STUB = """#!/bin/bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|-E) shift 2 ;;
    -n|-u|-s|-x) shift ;;
    *) break ;;
  esac
done
shift
exec "$@"
"""

# The `case` arm every `uv` stub carries for the wrapper's `deploy_locks.py plan` call. It
# runs the REAL module -- `shift 2` drops `run python`, and the interpreter is the one running
# the tests -- rather than scripting a lock list: a stub that printed its own order would make
# the concurrency tests flock whatever the stub said, and prove nothing about the deployer's
# names. `deploy_sh_env` sets DEPLOY_TEST_PYTHON. A test that wants a BROKEN plan writes its
# own arm ahead of this one.
UV_DEPLOY_LOCKS_ARM = '  *deploy_locks.py*) shift 2; exec "$DEPLOY_TEST_PYTHON" "$@" ;;'

# What a stubbed `ansible-playbook` must print for `deploy.sh` to count the run as a deploy.
# The wrapper reads the PLAY RECAP and refuses (exit 78) when it names no host -- ansible
# exits 0 for that -- so a stub that exits 0 in silence is a no-host run, not a success. One
# host line, in ansible's own layout, is the whole requirement. `test_deploy_exit_codes.py`
# holds the red half, where a stub prints the banner alone.
FAKE_RECAP = (
    'echo "PLAY RECAP *********"; '
    'echo "daniel-box                 : ok=3    changed=1    unreachable=0    failed=0"'
)


def git_free_env(**overrides: str) -> dict[str, str]:
    """`os.environ` with every `GIT_*` variable removed, plus `overrides`.

    Under a prek hook the environment carries `GIT_DIR` and `GIT_INDEX_FILE` pointing at the
    REAL repository, and those beat the child's working directory — so a run that looks scoped
    to a tmp_path would snapshot, and create worktrees in, this checkout.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(overrides)
    return env


def deploy_sh_env(tmp_path: Path, bin_dir: Path, **overrides: str) -> dict[str, str]:
    """The environment a `deploy.sh` run needs to stay inside `tmp_path`.

    The TREE lock is redirected too. `deploy.sh` takes it for real, for the snapshot, and the
    production path is the one a live gitops tick and the weekly secret-rotate cron hold — so a
    test taking it would queue behind real work and hold real work up behind itself.

    Args:
        tmp_path: the test's own directory; the snapshot root and lock directory go under it.
        bin_dir: the stub directory to put first on PATH.
        overrides: extra variables, applied last.
    """
    locks = tmp_path / "locks"
    locks.mkdir(exist_ok=True)
    return git_free_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HOMELAB_DEPLOY_SNAPSHOT_ROOT=str(tmp_path / "snapshots"),
        HOMELAB_DEPLOY_LOCK_DIR=str(locks),
        HOMELAB_DEPLOY_TREE_LOCK=str(locks / "server-git-tree.lock"),
        DEPLOY_TEST_PYTHON=sys.executable,
        **overrides,
    )


def make_snapshot_repo(path: Path) -> Path:
    """A one-commit git repo `deploy.sh` can snapshot HEAD from; returns `path`.

    Carries `ansible/deploy.yml` because that is the argument the wrapper hands
    ansible-playbook. The content never matters: every test that uses this stubs `uv`. Carries
    the real `deploy_locks.py` too, because the wrapper reads its lock list from that module
    at a checkout-relative path and the stubbed `uv` (UV_DEPLOY_LOCKS_ARM) runs it for real.
    """
    path.mkdir(parents=True, exist_ok=True)
    env = git_free_env()
    for args in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "deploy-sh-tests@example.invalid"),
        ("git", "config", "user.name", "deploy.sh tests"),
        ("git", "config", "commit.gpgsign", "false"),
    ):
        subprocess.run(args, cwd=path, env=env, check=True, capture_output=True)
    (path / "ansible").mkdir(exist_ok=True)
    (path / "ansible" / "deploy.yml").write_text("---\n[]\n")
    (path / DEPLOY_LOCKS_REL).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / DEPLOY_LOCKS_REL, path / DEPLOY_LOCKS_REL)
    subprocess.run(
        ("git", "add", "-A"), cwd=path, env=env, check=True, capture_output=True
    )
    subprocess.run(
        ("git", "commit", "-q", "-m", "seed", "--no-gpg-sign"),
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
    )
    return path


def stub_bin(tmp_path: Path, stubs: dict[str, str]) -> Path:
    """Write each stub as an executable under `tmp_path/bin` and return that directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def stub_path(tmp_path: Path, stubs: dict[str, str]) -> dict[str, str]:
    """`os.environ` with the stubs from `stub_bin` ahead of everything on PATH."""
    bin_dir = stub_bin(tmp_path, stubs)
    return dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")


# What `deploy.sh --detach` prints once it has backgrounded the playbook subshell.
_DETACHED_PID = re.compile(r"running in background \(pid (\d+)\)")


def detached_pid(output: str) -> int:
    """The pid of the subshell a `--detach` run backgrounded, from the wrapper's own output.

    That subshell runs the playbook, the notifier and `remove_snapshot`, then exits — so its
    exit is the event a test waits on for anything the detached half does, via
    `_process_waits.wait_for_exit`, rather than polling for its side effects.
    """
    match = _DETACHED_PID.search(output)
    assert match, f"deploy.sh --detach printed no background pid:\n{output}"
    return int(match.group(1))
