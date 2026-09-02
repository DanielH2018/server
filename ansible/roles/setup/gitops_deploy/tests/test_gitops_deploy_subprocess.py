"""What gitops_deploy.py execs, and how it stops what it exec'd.

`deploy_k8s()`'s argv is pinned byte-for-byte, and `run()`'s timeout must kill the whole
process group or a wedged ansible-playbook outlives the tick. The rollback call site in
main() is exercised in test_gitops_deploy_main_branches.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_subprocess.py

import os
import subprocess
import time

import pytest


# ── deploy_k8s ────────────────────────────────────────────────────────────────────────────────
def _capture_run(gitops_deploy, monkeypatch):
    """Patch gitops_deploy.run() to record every call instead of shelling out, and return the
    list it appends to."""

    class _Call:
        def __init__(self, argv, kwargs):
            self.argv = argv
            self.kwargs = kwargs

    calls: list[_Call] = []

    def _fake_run(argv, **kwargs):
        calls.append(_Call(argv, kwargs))
        return ""

    monkeypatch.setattr(gitops_deploy, "run", _fake_run)
    return calls


_FORWARD_ARGV = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]


def test_deploy_k8s_passes_no_extra_vars_by_default(gitops_deploy, monkeypatch) -> None:
    """The ordinary deploy must be byte-identical to what it was before this slice.

    ~50 services go through this call on every tick. Pins the full argv, not just -e's absence — a
    stray extra arg anywhere else in the list would pass a presence-only check.
    """
    calls = _capture_run(gitops_deploy, monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0)
    assert calls[0].argv == _FORWARD_ARGV


def test_deploy_k8s_passes_the_restore_sha_when_given(
    gitops_deploy, monkeypatch
) -> None:
    calls = _capture_run(gitops_deploy, monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="deadbeef")
    assert calls[0].argv == _FORWARD_ARGV + ["-e", "k8s_restore_snapshot_sha=deadbeef"]


def test_deploy_k8s_treats_a_whitespace_only_restore_sha_as_absent(
    gitops_deploy, monkeypatch
) -> None:
    """restore_sha="" or all-whitespace must stay inert, matching the manifests role's own
    `| trim | length > 0` guard — a blank-but-truthy string must not add a broken `-e` arg."""
    calls = _capture_run(gitops_deploy, monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="   ")
    assert calls[0].argv == _FORWARD_ARGV


# ── run()'s timeout must kill the whole process group ───────────────────────────────────────────
# `uv run ansible-playbook ...` is a GRANDCHILD of run()'s subprocess (uv forks it rather than
# exec'ing into it). `subprocess.run(timeout=)` DOES return promptly on timeout — its internal
# communicate() raises on the wall-clock deadline, not on pipe EOF — but it kills only the DIRECT
# child (uv). Verified empirically against the pre-fix implementation: the call returns on time
# and the grandchild is still alive at that moment, left running as an orphan with nothing
# watching it. That is how K8S_ROLLBACK_TIMEOUT_S stopped being an actual bound on the underlying
# ansible-playbook: gitops_deploy.py moves on while the timed-out run keeps mutating the cluster,
# and the real stop becomes systemd's TimeoutStartSec SIGTERM against the wrapping unit, which can
# land mid-rollback. This shape reproduces it directly: a shell script backgrounds a grandchild
# that outlives a naive kill-the-direct-child-only fix, so the test fails against the OLD run()
# and passes only once the whole process group is killed.
_GRANDCHILD_SHAPE = """#!/bin/sh
sh -c 'echo $$ > "{pidfile}"; sleep 30' &
wait
"""


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_timeout_kills_the_whole_process_group(gitops_deploy, tmp_path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.sh"
    script.write_text(_GRANDCHILD_SHAPE.format(pidfile=pidfile))
    script.chmod(0o755)

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        gitops_deploy.run(["sh", str(script)], cwd=str(tmp_path), timeout=1.0)
    elapsed = time.monotonic() - start

    # Both the buggy and the fixed run() return around the 1.0s deadline — the deadline is a
    # wall-clock check inside communicate(), not a wait for pipe EOF, so this alone does not
    # discriminate them. It is here as a sanity bound; the real regression check is the
    # grandchild-liveness assert below.
    assert elapsed < 10, (
        f"run() took {elapsed:.1f}s to return after a 1.0s timeout — expected it to return "
        f"around the deadline regardless of whether the fix is applied"
    )

    deadline = time.monotonic() + 2
    grandchild_pid = None
    while grandchild_pid is None and time.monotonic() < deadline:
        if pidfile.exists():
            grandchild_pid = int(pidfile.read_text().strip())
        else:
            time.sleep(0.05)
    assert grandchild_pid is not None, "the grandchild never started"

    # SIGKILL is instant but reaping is not: once its own parent (the script) is also killed,
    # the grandchild is reparented and reaped by the nearest subreaper — poll briefly instead
    # of asserting the instant killpg returns.
    deadline = time.monotonic() + 3
    while _pid_is_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_alive(grandchild_pid), (
        f"grandchild pid {grandchild_pid} outlived the timeout — only the direct child was "
        f"killed, not its process group"
    )
