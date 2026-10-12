"""`running_batch_holds`: which stale claims a still-running fan-out batch keeps live.

The runner is faked, so no test reads a real unit or opens an ssh connection. A failed or
unparseable read must hold the claim, never release it.
"""

import subprocess

from dev.fanout_lib.batch_holds import running_batch_holds
from dev.fanout_lib.manifest import Batch, Manifest, save

SERVER = "DanielH2018/server"


def _batch(batch, host="daniel-box", *, issues, repo=SERVER, removed_at=None):
    return Batch(
        batch,
        host,
        f"/w/{batch}",
        f"worktree-fanout-{batch}",
        f"fanout-{batch}.service",
        issues,
        "t",
        removed_at,
        repo,
    )


def _root(tmp_path, *batches):
    save(Manifest("20261012T000000Z", "worktree-orch", list(batches)), tmp_path)
    return tmp_path


def _runner(stdout="", returncode=0, stderr="", raises=None):
    calls = []

    def run(host, command, timeout):
        calls.append((host, command))
        if raises:
            raise raises
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    return run, calls


def test_an_issue_whose_batch_unit_is_active_is_held(tmp_path):
    run, _ = _runner("active\n")
    root = _root(tmp_path, _batch("7", issues=[7]))
    assert "still running" in running_batch_holds(SERVER, [7], root, run)[7]


def test_an_issue_whose_batch_unit_has_finished_is_stale(tmp_path):
    run, _ = _runner("inactive\n", returncode=3)
    root = _root(tmp_path, _batch("7", issues=[7]))
    assert running_batch_holds(SERVER, [7], root, run) == {}


def test_an_unreachable_peer_host_is_held_as_cannot_tell(tmp_path):
    run, _ = _runner(returncode=255, stderr="ssh: connect to host daniel-server")
    root = _root(tmp_path, _batch("7", "daniel-server", issues=[7]))
    assert "cannot tell" in running_batch_holds(SERVER, [7], root, run)[7]


def test_an_empty_read_is_held_as_cannot_tell_not_read_as_inactive(tmp_path):
    # The local leg with no user bus prints nothing on stdout and exits 1.
    run, _ = _runner(returncode=1, stderr="Failed to connect to bus")
    root = _root(tmp_path, _batch("7", issues=[7]))
    assert "cannot tell" in running_batch_holds(SERVER, [7], root, run)[7]


def test_a_timed_out_read_is_held_as_cannot_tell(tmp_path):
    run, _ = _runner(raises=subprocess.TimeoutExpired("ssh", 10))
    root = _root(tmp_path, _batch("7", "daniel-server", issues=[7]))
    assert "cannot tell" in running_batch_holds(SERVER, [7], root, run)[7]


def test_removed_other_repo_and_unasked_batches_run_nothing(tmp_path):
    root = _root(
        tmp_path,
        _batch("7", issues=[7], removed_at="t"),
        _batch("8", issues=[7], repo="DanielH2018/dotfiles"),
        _batch("9", issues=[9]),
    )
    run, calls = _runner("active\n")
    assert running_batch_holds(SERVER, [7], root, run) == {}
    assert calls == []


def test_one_read_per_host_covers_every_batch_on_it(tmp_path):
    root = _root(tmp_path, _batch("7", issues=[7]), _batch("8-9", issues=[8, 9]))
    run, calls = _runner("inactive\nactive\n")
    assert sorted(running_batch_holds(SERVER, [7, 8, 9], root, run)) == [8, 9]
    assert len(calls) == 1
    assert "fanout-7.service fanout-8-9.service" in calls[0][1]
