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
import subprocess
from pathlib import Path

# The shared `flock` stub: drop flock's own options and its lock-file argument, then run
# whatever is left. No real lock is taken, so no test can interleave with a live deploy.
# `-s` is in the single-shift arm because a scoped deploy takes `server-deploy-all.lock`
# shared; without it the stub tries to exec `-w`.
FLOCK_STUB = """#!/bin/bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|-E) shift 2 ;;
    -n|-u|-s) shift ;;
    *) break ;;
  esac
done
shift
exec "$@"
"""


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
        **overrides,
    )


def make_snapshot_repo(path: Path) -> Path:
    """A one-commit git repo `deploy.sh` can snapshot HEAD from; returns `path`.

    Carries `ansible/deploy.yml` because that is the argument the wrapper hands
    ansible-playbook. The content never matters: every test that uses this stubs `uv`.
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
