#!/usr/bin/env python3
"""The wait lines `deploy.sh` prints, and that land.py books them.

`land_lib/landing.py:retry_while_locked` books a wait only when an attempt EXITS 75. Both
wrappers can wait a long time and exit 0 instead -- deploy.sh inside `flock -w LOCK_WAIT`,
gitops_tick.sh watching a tick another actor started (`test_gitops_tick_wrapper.py`) -- so over the 14 days to 2026-09-11
every ledger row read `lock=0` while the tree lock was busy 17% of one day. These are the
lines that close that gap, asserted together with the parser that reads them: a wording
change on either side that the other does not follow is exactly the drift this file catches.

No test here touches /var/lock/server-git-tree.lock, the real systemd units, or the host's
syslog. A foreground deploy takes real flock(2) locks on tmp_path files, contended by a
real `flock` holder, in both the foreground and `--detach`. `fuser`,
`ps`, `uv`, `logger`, `systemctl` and `journalctl` are stubbed on PATH. The only live reads are
gitops_tick.sh's own `/var/lib/gitops-deploy` markers.

Run: uv run pytest scripts/deploy_tools/tests/test_wrapper_lock_wait_lines.py
"""

import contextlib
import fcntl
import os
import shutil
import subprocess
import time
from pathlib import Path

from _deploy_sh_fakes import (
    FAKE_RECAP,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
    stub_bin,
)
from deploy_tools import exit_codes as ec
from deploy_tools.land_lib import tools

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"
_TICK_SH = _REPO / "scripts" / "deploy_tools" / "gitops_tick.sh"


def _deploy_repo_env(tmp_path: Path, bin_dir: Path) -> tuple[Path, dict[str, str]]:
    """A throwaway repo for deploy.sh to snapshot, and the env that keeps the run inside it.

    deploy.sh now copies HEAD into a detached worktree before it runs the playbook. Run
    against this checkout, every test here would register a real worktree under the real
    `.git`; `git_free_env` is what stops a prek hook's `GIT_DIR` overriding `cwd` and doing
    that anyway. The snapshot root and the per-service lock directory are redirected for the
    same reason: nothing here may write under /var/lock or /tmp/homelab-deploy-snapshots.
    """
    return make_snapshot_repo(tmp_path / "repo"), deploy_sh_env(tmp_path, bin_dir)


# Records how many descriptors the CALLER already has on the lock file, which is the thing
# real `fuser` would have reported as a holder. Measured on 2026-09-11: `fuser` scans every
# process's descriptors, so a deploy.sh that opens the lock before sampling reports itself --
# and `fuser "$LOCK" {fd}>&-` does not help, because the parent shell still holds it.
_FUSER = """#!/bin/bash
ls -l "/proc/$PPID/fd" 2>/dev/null |
  awk '/server-git-tree.lock/ { n++ } END { print n + 0 }' >"$FUSER_STUB_SELF_FDS"
echo "  4242"
"""

_PS = """#!/bin/bash
echo "   99 uv run ansible-playbook ansible/deploy.yml --tags sonarr"
"""

# Prints a one-host recap: the wrapper reads it, and an exit 0 in silence is a no-host run.
_UV = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)

# `_UV` exits 0 for the playbook, so deploy.sh reaches `emit_deploy_annotation`, which writes
# an `event=deploy` line through `logger`. conftest's autouse `_no_syslog` already intercepts
# that directory-wide, and measurement confirms no test run reached /var/log/syslog. This stub
# is here so the property does not depend on a fixture in another file: a test that writes to
# the host's syslog lands on the real Deploys board beside real deploys.
_LOGGER = """#!/bin/bash
exit 0
"""


def _run_deploy(tmp_path: Path, **env_extra: str) -> subprocess.CompletedProcess:
    """A foreground deploy. Its locks are real flock(2) on tmp_path files (#2412 slice 3)."""
    bin_dir = stub_bin(
        tmp_path, {"fuser": _FUSER, "ps": _PS, "uv": _UV, "logger": _LOGGER}
    )
    repo, env = _deploy_repo_env(tmp_path, bin_dir)
    env["FUSER_STUB_SELF_FDS"] = str(tmp_path / "self-fds")
    env.update(env_extra)
    return subprocess.run(
        [
            str(_DEPLOY_SH),
            "--tags",
            "uptime-kuma",
            "--skip-tag-check",
            "--skip-staleness-check",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@contextlib.contextmanager
def _held(path: Path, seconds: float):
    """Another process holding `path` for `seconds`, with the lock taken before this yields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [shutil.which("flock") or "flock", str(path), "sleep", str(seconds)]
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    break
                time.sleep(0.02)
        finally:
            os.close(fd)
        yield
    finally:
        holder.kill()
        holder.wait()


def test_a_contended_acquire_is_reported_with_its_seconds_and_its_holder(tmp_path):
    """FLAGGED half: the wait deploy.sh rides out on the tree lock must reach the log."""
    with _held(tmp_path / "locks" / "server-git-tree.lock", 1.5):
        result = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    line = next((x for x in result.stderr.splitlines() if "lock acquired" in x), "")
    assert line, result.stderr
    assert "(holder was: pid 4242" in line
    assert "ansible-playbook ansible/deploy.yml --tags sonarr" in line
    booked = tools.in_flock_wait(line)
    assert booked is not None, f"land.py no longer parses deploy.sh's line: {line!r}"
    assert booked[0] >= 1
    assert (tmp_path / "self-fds").read_text().strip() == "0", (
        "deploy.sh held the lock file when it sampled the holder, so real `fuser` would "
        "have reported deploy.sh itself and the landing would name itself as its blocker"
    )


def test_an_uncontended_acquire_says_nothing(tmp_path):
    """CLEAN half: a line on every deploy would make `lock=0` rows unreadable as evidence.

    A lock taken at once reports no wait whatever the clock says (#1881): `flock -n`
    succeeding is 0s by construction, so a slow fork cannot book a phantom `lock=1`.
    """
    result = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "lock acquired" not in result.stderr
    assert "service lock" not in result.stderr


def test_a_contended_service_lock_reports_its_tag_and_its_seconds(tmp_path):
    """FLAGGED half: most of what a landing waits for is now THIS lock, not the tree lock.

    Without this line a landing behind another deploy of the same service books `lock=0` and
    charges the wait to `deploy` — the same gap the tree-lock line closed for the tree lock.
    """
    with _held(tmp_path / "locks" / "server-deploy-uptime-kuma.lock", 1.5):
        result = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    line = next((x for x in result.stderr.splitlines() if "service lock" in x), "")
    assert line, result.stderr
    assert "service lock uptime-kuma acquired after" in line
    booked = tools.in_flock_wait(line)
    assert booked is not None, f"land.py no longer parses deploy.sh's line: {line!r}"
    assert booked[0] >= 1
    # The tree lock was free, so nothing may be booked against it.
    assert "deploy: lock acquired after" not in result.stderr


def test_the_service_lock_refusal_is_not_booked_as_a_wait():
    """CLEAN half for the parser itself: only an ACQUIRE reports time this landing waited.

    The refusal line names ${LOCK_WAIT} seconds. A pattern loose enough to match it would book
    the whole budget onto a landing that deployed nothing, which is the opposite of what
    `lock=` means.
    """
    refusal = "deploy: a service lock under /var/lock stayed busy for 3000s -- nothing"
    assert tools.in_flock_wait(refusal) is None
    assert (
        tools.in_flock_wait("deploy: service lock sonarr acquired after 4s ok") is None
    )


def test_a_lock_timeout_is_still_reported_as_contention(tmp_path):
    """CLEAN half for exit 75: the wait really did elapse, so nothing was deployed."""
    with _held(tmp_path / "locks" / "server-git-tree.lock", 10):
        result = _run_deploy(tmp_path, HOMELAB_DEPLOY_LOCK_WAIT="1")
    assert result.returncode == 75, result.stderr
    assert "nothing was deployed" in result.stderr
    assert "A deploy is already running" in result.stderr


def _unopenable_tree_lock(tmp_path: Path) -> str:
    """A tree-lock path that is a directory: open(2) refuses it, and nothing holds it."""
    path = tmp_path / "locks" / "server-git-tree.lock"
    path.mkdir(parents=True)
    return str(path)


def test_any_other_flock_failure_is_not_reported_as_contention(tmp_path):
    """FLAGGED half: reading every lock failure as a busy lock.

    That tells an operator "a deploy is already running, retry shortly" for a lock file the
    wrapper could not open at all, which is a resume point that never resumes.
    """
    result = _run_deploy(
        tmp_path, HOMELAB_DEPLOY_TREE_LOCK=_unopenable_tree_lock(tmp_path)
    )
    assert result.returncode != 75, result.stderr
    assert "A deploy is already running" not in result.stderr


def test_a_flock_failure_that_is_not_contention_exits_its_own_code(tmp_path):
    """FLAGGED half for issue #1775: it used to fall through to 20.

    20 promises "a task failed AFTER applying; some changes are live", which land.py prints
    verbatim — for a run that never started ansible. 76 says what happened instead.
    """
    result = _run_deploy(
        tmp_path, HOMELAB_DEPLOY_TREE_LOCK=_unopenable_tree_lock(tmp_path)
    )
    assert result.returncode == ec.DEPLOY_LOCK_UNAVAILABLE, result.stderr
    assert "Is a directory" in result.stderr
    assert "the playbook ran and failed" not in result.stderr
    assert ec.DEPLOY_LOCK_UNAVAILABLE in ec.DEPLOY_SH_NO_VERDICT


# `--detach` takes the tree lock NON-blocking, so a held one refuses at once rather than
# queueing, and the same pair applies: contention is 75, a lock file it cannot open is 76.
def _run_detach(tmp_path: Path, **env_extra: str) -> subprocess.CompletedProcess:
    bin_dir = stub_bin(
        tmp_path, {"fuser": _FUSER, "ps": _PS, "uv": _UV, "logger": _LOGGER}
    )
    repo, env = _deploy_repo_env(tmp_path, bin_dir)
    env["FUSER_STUB_SELF_FDS"] = str(tmp_path / "self-fds")
    env.update(env_extra)
    return subprocess.run(
        [
            str(_DEPLOY_SH),
            "--detach",
            "--tags",
            "uptime-kuma",
            "--skip-tag-check",
            "--skip-staleness-check",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_detach_still_reports_a_held_lock_as_contention(tmp_path):
    """CLEAN half: --detach fails fast on a real holder, and 75 says retry shortly."""
    with _held(tmp_path / "locks" / "server-git-tree.lock", 10):
        result = _run_detach(tmp_path)
    assert result.returncode == ec.DEPLOY_LOCK_BUSY, result.stderr
    assert "A deploy is already running" in result.stderr
    assert "running in background" not in result.stdout


def test_detach_tells_a_broken_lock_file_from_a_held_one(tmp_path):
    """FLAGGED half: `flock -n` without `-E` answered 1 for both, and this arm read both
    as contention."""
    result = _run_detach(
        tmp_path, HOMELAB_DEPLOY_TREE_LOCK=_unopenable_tree_lock(tmp_path)
    )
    assert result.returncode == ec.DEPLOY_LOCK_UNAVAILABLE, result.stderr
    assert "Is a directory" in result.stderr
    assert "A deploy is already running" not in result.stderr


def test_detach_reports_a_busy_service_lock_as_contention(tmp_path):
    """FLAGGED half for the bash `$?`-inside-`if !` read: contention exited 76, not 75.

    The service locks WAIT under --detach, as in the foreground, and a wait that runs out is
    75: another deploy of the same service holds the lock and will release it, so retrying is
    the whole remedy. 76 would tell the session retrying changes nothing.
    """
    with _held(tmp_path / "locks" / "server-deploy-uptime-kuma.lock", 10):
        result = _run_detach(tmp_path, HOMELAB_DEPLOY_LOCK_WAIT="1")
    assert result.returncode == ec.DEPLOY_LOCK_BUSY, result.stderr
    assert "deploy --detach: a deploy of one of these services held its lock" in (
        result.stderr
    )
